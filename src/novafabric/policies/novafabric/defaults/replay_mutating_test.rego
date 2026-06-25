package novafabric.defaults.replay_mutating_test

import data.novafabric.defaults.replay_mutating

test_admin_allowed if {
    replay_mutating.allow with input as {"subject": {"roles": ["admin"]}}
}

test_writer_denied if {
    not replay_mutating.allow with input as {"subject": {"roles": ["writer"]}}
}
