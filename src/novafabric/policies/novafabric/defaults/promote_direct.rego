package novafabric.defaults.promote_direct

# ADR-0312 D1: single-actor `nova promote direct` into a protected stage needs an
# explicit policy allow. NovaFabric evaluates this package only when the target is a
# protected stage (`registry.service.PROTECTED_STAGES`, `production` today) and passes
# the target as input.resource.to_status. The default denies; the two-person path
# (`nova promote propose` + `nova promote approve`) is not affected.
#
# To permit direct promotion for the cases you accept, ship your own bundle
# (NOVAFABRIC_POLICY_BUNDLE_PATH) whose `allow` is true for them. Note that
# input.subject.user is not an authenticated identity on the CLI path.

default allow := false

default reason := "direct promotion into a protected stage is denied by default; use nova promote propose + nova promote approve"

protected_stages := {"production"}

protected if input.resource.to_status in protected_stages

allow if not protected

reason := "target stage is not protected" if not protected
