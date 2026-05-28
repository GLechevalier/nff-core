from nff_diagnosis.engine.diagnosis import DiagnosisEngine
from nff_diagnosis.resolution.addr2line import Addr2LineResolver
from nff_diagnosis.schemas import CrashRecord
from nff_diagnosis.schemas.diagnosis_output import DiagnosisOutput
from nff_diagnosis.storage.base import StorageBase
from nff_diagnosis.store.base import ELFStoreBase


def run_pipeline(
    crash: CrashRecord,
    store: ELFStoreBase,
    storage: StorageBase,
    engine: DiagnosisEngine,
) -> DiagnosisOutput:
    storage.save_crash(crash)
    elf_path = store.get_elf_path(crash.build_id)
    resolver = Addr2LineResolver()
    resolved_bt, location = resolver.resolve(crash, elf_path)
    diag = engine.diagnose(crash, resolved_bt, location)
    storage.save_diagnosis(diag)
    return diag
