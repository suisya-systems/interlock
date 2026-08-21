"""Gate item 2 (issue #18): single-writer re-identification across the crash window.

Two layers, deliberately separated (the distinction is load-bearing -- see the
issue): the *Interlock-mediated proof* runs the crash-and-retry shapes through
the control plane, where the losing claimant must never become a process; the
*unmediated characterisation* drives the real CLI directly and lives in
``investigation/`` (it is where two processes on one id are allowed to appear,
because that is the fact being measured). Nothing in this package imports a
measured window width as a constant (U34).
"""
