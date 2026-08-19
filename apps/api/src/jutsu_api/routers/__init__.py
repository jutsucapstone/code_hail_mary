"""HTTP routers.

Every router is constructed with `route_class=GuardedAPIRoute`, so registering an
endpoint without `@requires(...)` or `@public(...)` raises while the module is imported.
The app cannot start with an unguarded route.
"""
