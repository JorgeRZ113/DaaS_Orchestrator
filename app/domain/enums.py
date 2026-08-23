"""Vocabulario de estados del ciclo de vida de una ejecucion."""

from enum import Enum


class ExecutionState(str, Enum):
    # --- Estados que el pipeline produce (los 10 operativos) ---
    pending = "PENDING"
    validating = "VALIDATING"
    deploying = "DEPLOYING"
    tn_ready = "TN_READY"
    running_experiment = "RUNNING_EXPERIMENT"
    collecting = "COLLECTING"
    # La TN sigue viva y 'activated' en TNLCM: lo unico que se ha bajado es el
    # tunel WireGuard, para poder trabajar con otra TN. Se vuelve a TN_READY con
    # POST /executions/{id}/resume, sin redesplegar ni tocar el descriptor.
    paused = "PAUSED"
    destroying = "DESTROYING"
    destroyed = "DESTROYED"
    failed = "FAILED"

    # --- Estados declarados que NINGUNA transicion asigna ---
    # Ojo al leer el enum: los dos de abajo se publican en el esquema OpenAPI a
    # traves de `ExecutionResponse.status`, asi que un cliente los ve como
    # valores posibles aunque el pipeline no los emita nunca.

    # Legacy: solo para deserializar executions.json anteriores al rediseno del
    # ciclo de vida. Su papel lo cumplen hoy TN_READY y DESTROYED.
    completed = "COMPLETED"

    # Reservado para la operacion de cancelacion, que NO esta implementada: no
    # hay endpoint ni transicion que lo asigne. No confundir con el "Cancelled"
    # que si se maneja, que es el estado remoto del experimento en ELCM y
    # desemboca en TN_READY con `error`, no en este miembro.
    cancelled = "CANCELLED"
