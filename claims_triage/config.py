from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]

@dataclass(frozen=True)
class Settings:
    backend: str = "mock"
    state_dir: Path = ROOT / ".runtime"
    project_endpoint: str = ""
    model_deployment: str = ""
    timeout: float = 90
    data_dir: Path = ROOT / "data"
    knowledge_dir: Path = ROOT / "knowledge"

    @classmethod
    def load(cls, backend=None, state_dir=None):
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            backend=backend or os.getenv("TRIAGE_BACKEND", "mock"),
            state_dir=Path(state_dir or os.getenv("TRIAGE_STATE_DIR", str(ROOT / ".runtime"))).resolve(),
            project_endpoint=os.getenv("AZURE_AI_PROJECT_ENDPOINT", ""),
            model_deployment=os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", ""),
            timeout=float(os.getenv("TRIAGE_RUN_TIMEOUT_SECONDS", "90")),
        )
