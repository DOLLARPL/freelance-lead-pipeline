#!/usr/bin/env bash
# Domkniecie wdrozenia snajper.jnxautomation.pl po stronie DNS.
#
# Rekord A w home.pl dodaje czlowiek (panel klienta nie ma API), wiec ten skrypt
# sprawdza co 5 minut, czy snajper.jnxautomation.pl wskazuje juz na ten serwer.
# Gdy wskaze:
#   1. przelacza Caddyfile na wersje docelowa (80 -> 443, bez tymczasowego bloku http),
#   2. restartuje Caddy i czeka na certyfikat Let's Encrypt,
#   3. sprawdza HTTPS, naglowki i przekierowanie logowania,
#   4. zapisuje wynik w /opt/data/cache/snajper-web/dns-flip.log i stawia flage.
# Flaga dns-flip.done blokuje powtorki.
set -uo pipefail

HOST="snajper.jnxautomation.pl"
IP="203.0.113.10"
FLAGA=/opt/data/cache/snajper-web/dns-flip.done
LOG=/opt/data/cache/snajper-web/dns-flip.log
CADDY=/etc/caddy/Caddyfile

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

[ -f "$FLAGA" ] && exit 0

rozwiazane=$(dig +short "$HOST" A @1.1.1.1 2>/dev/null | tr '\n' ' ')
case "$rozwiazane" in
  *"$IP"*) : ;;
  *) exit 0 ;;
esac

echo "$(stamp) DNS wskazuje na $IP ($rozwiazane), przelaczam Caddy" >>"$LOG"

cat >"$CADDY" <<'EOF'
# Sniper ofert: publiczny panel pod snajper.jnxautomation.pl.
# Jedyny backend to 127.0.0.1:8787. Naglowek tozsamosci z Tailscale jest tu
# usuwany, zeby nikt z zewnatrz nie udawal urzadzenia w tailnecie.
{
	email jan@jnxautomation.pl
}

snajper.jnxautomation.pl {
	bind 203.0.113.10
	encode zstd gzip
	header {
		Strict-Transport-Security "max-age=31536000; includeSubDomains"
		X-Robots-Tag "noindex, nofollow, noarchive"
		X-Content-Type-Options "nosniff"
		X-Frame-Options "SAMEORIGIN"
		Referrer-Policy "strict-origin-when-cross-origin"
		-Server
	}
	reverse_proxy 127.0.0.1:8787 {
		header_up -Tailscale-User-Login
		header_up X-Real-IP {remote_host}
	}
	log {
		output file /var/log/caddy/snajper.log {
			roll_size 10MiB
			roll_keep 5
		}
		format json
	}
}

:80 {
	redir https://{host}{uri} 308
}
EOF

caddy validate --config "$CADDY" >>"$LOG" 2>&1
systemctl restart caddy
sleep 25

kod=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "https://$HOST/login" || echo "000")
przekier=$(curl -s -o /dev/null -w "%{http_code} %{redirect_url}" --max-time 20 "https://$HOST/" || echo "000")
hsts=$(curl -sI --max-time 20 "https://$HOST/login" | tr -d '\r' | grep -i "strict-transport-security" | head -1)
cert=$(echo | openssl s_client -connect "$IP:443" -servername "$HOST" 2>/dev/null | openssl x509 -noout -issuer -enddate 2>/dev/null | tr '\n' ' ')

echo "$(stamp) login=$kod root='$przekier' hsts='${hsts:-brak}' cert='${cert:-brak}'" >>"$LOG"

if [ "$kod" = "200" ]; then
  echo "$(stamp) OK, HTTPS dziala" >>"$LOG"
  echo "$(stamp) $(echo "$przekier" | head -1)" >>"$LOG"
  touch "$FLAGA"
else
  echo "$(stamp) HTTPS jeszcze nie wstal, kolejna proba za 5 min" >>"$LOG"
fi
