package novafabric.defaults.replay_mutating

default allow := false

allow if {
    "admin" in input.subject.roles
}
