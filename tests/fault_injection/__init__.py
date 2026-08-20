"""S9 -- the fault-injection harness (Issue ``#15``).

The design this package implements is ``docs/s9-fault-injection-harness.md``.
Read that first: every module here names the section it discharges, and the
section numbers -- not line numbers -- are the stable cross-reference.

Layout, per design section 6.1::

    contract.py     the fault-runner contract: vocabulary, versions, invariant
                    names, protocol messages, driver CLI            -- DURABLE
    controller.py   spawn / barrier / kill / restart / cleanup engine -- DURABLE
    manifest.py     the case matrix generator and its frozen literal  -- DURABLE
    manifest.json   the frozen matrix                                 -- DURABLE
    conformance.py  the adapter conformance battery                   -- DURABLE
    test_*.py       the cases                                         -- DURABLE
    spike_driver.py the role driver binding the contract to S6/S7  -- THROWAWAY

``spike_driver.py`` is the only module in this tree allowed to import
``claude_org_runtime.control_plane``; ``test_import_graph.py`` enforces it, so
the coupling to the spike internals cannot spread by convenience and the
durable half survives the S5-S7 discard (D-0026).
"""
