"""Errores del ciclo de vida que la capa HTTP traduce a codigos de estado.

Viven aparte para que cualquier fase pueda lanzarlos sin importar el coordinador
—lo que crearia un ciclo— y para que la correspondencia con el codigo HTTP se
lea de un vistazo.
"""


class TnlcmDeploymentInProgressError(RuntimeError):
    """Raised when a TNLCM deployment is already running."""


class ExecutionNotFoundError(LookupError):
    """Raised when the referenced execution_id does not exist."""


class ExecutionConflictError(RuntimeError):
    """Raised when the execution state does not allow the requested operation (HTTP 409)."""


class PhaseStillRunningError(TimeoutError):
    """Raised when a blocking endpoint gives up waiting for its phase (HTTP 504)."""
