"""Hospital system adapters: Bahmni, plain FHIR R4, and no HMS at all."""

from .bahmni import BahmniHospital
from .desk import DeskHospital
from .fhir import FhirHospital

__all__ = ["BahmniHospital", "DeskHospital", "FhirHospital"]
