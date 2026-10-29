# Uncertain launch regression

The registry previously released a committed reservation whenever its native
launcher raised. A transport timeout may occur after the remote process starts,
so that release could admit competing work while the original execution lived.

The registry now retains the committed reservation on launcher exceptions.
Retrying a committed request requires reconciliation and never invokes the
launcher again. Capacity remains accounted for by the scheduler's committed
reservation accounting until an explicit release follows verified termination.

Verification: the adapter, scheduler, persistence, and service test selection
passed 17 tests; targeted Ruff and git diff whitespace checks passed.

This is a bounded correction. Earlier phase notes overstate the broader system:
the fault_matrix helper enumerates expected outcomes and is not fault injection;
the resume registry contains capability declarations and not certified native
restore implementations; event replay alone does not establish cross-process
atomic admission or enforced workspace fencing. These remain required work.
