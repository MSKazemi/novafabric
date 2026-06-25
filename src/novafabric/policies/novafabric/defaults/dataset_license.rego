package novafabric.defaults.dataset_license

import future.keywords.if

default allow := true

allow := false if {
    input.resource.dataset_expires_at != null
    input.resource.dataset_expires_at < input.context.timestamp
}
