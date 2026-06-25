package novafabric.defaults.regression_gate

default allow := false
default reason := "regression detected or report missing"

allow if {
    input.resource.regression_report.regression_detected == false
}

reason := "regression detected in eval comparison" if {
    input.resource.regression_report.regression_detected == true
}

reason := "regression_report not present in policy input" if {
    not input.resource.regression_report
}
