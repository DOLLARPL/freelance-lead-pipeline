"""Specialist source adapters for Freelance Pipeline v2."""

from freelance_pipeline.sources.justjoinit import JustJoinITError, JustJoinITJobs
from freelance_pipeline.sources.make import MakeHireHelp, MakeHireHelpError
from freelance_pipeline.sources.n8n import N8NCommunityJobs, N8NCommunityJobsError
from freelance_pipeline.sources.n8ndevs import N8NDevsError, N8NDevsJobs
from freelance_pipeline.sources.nofluffjobs import NoFluffJobsError, NoFluffJobsJobs
from freelance_pipeline.sources.oferteo import OFERTEO_PRODUCTION_CATEGORY_PATHS, OferteoError, OferteoJobs
from freelance_pipeline.sources.oferteo_mail import OferteoMailError, OferteoMailJobs
from freelance_pipeline.sources.rocketjobs import RocketJobsError, RocketJobsJobs
from freelance_pipeline.sources.useme import UsemeError, UsemeJobs

__all__ = [
    "JustJoinITError",
    "JustJoinITJobs",
    "MakeHireHelp",
    "MakeHireHelpError",
    "N8NCommunityJobs",
    "N8NCommunityJobsError",
    "N8NDevsError",
    "N8NDevsJobs",
    "NoFluffJobsError",
    "NoFluffJobsJobs",
    "OFERTEO_PRODUCTION_CATEGORY_PATHS",
    "OferteoError",
    "OferteoJobs",
    "OferteoMailError",
    "OferteoMailJobs",
    "RocketJobsError",
    "RocketJobsJobs",
    "UsemeError",
    "UsemeJobs",
]