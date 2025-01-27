#!groovy

List<Integer> call(Map params, String region){
    // handle params which can be a json list
    def test_config = groovy.json.JsonOutput.toJson(params.test_config)
    def cmd = """
    #!/bin/bash
    export SCT_CLUSTER_BACKEND="${params.backend}"
    export SCT_CONFIG_FILES=${test_config}
    if [[ -n "${params.region ? params.region : ''}" ]] ; then
        export SCT_REGION_NAME=${groovy.json.JsonOutput.toJson(params.region)}
    fi

    if [[ -n "${params.gce_datacenter ? params.gce_datacenter : ''}" ]] ; then
        export SCT_GCE_DATACENTER=${groovy.json.JsonOutput.toJson(params.gce_datacenter)}
    fi

    if [[ -n "${params.azure_region_name ? params.azure_region_name : ''}" ]] ; then
        export SCT_AZURE_REGION_NAME=${groovy.json.JsonOutput.toJson(params.azure_region_name)}
    fi
    ./docker/env/hydra.sh output-conf -b "${params.backend}"
    """
    def testData = sh(script: cmd, returnStdout: true).trim()
    println(testData)
    testData = testData =~ /test_duration: (\d+)/
    testDuration = testData[0][1].toInteger()
    Integer testStartupTimeout = 20
    Integer testTeardownTimeout = 40
    Integer collectLogsTimeout = 90
    Integer resourceCleanupTimeout = 30
    Integer sendEmailTimeout = 5
    Integer testRunTimeout = testStartupTimeout + testDuration + testTeardownTimeout
    Integer runnerTimeout = testRunTimeout + collectLogsTimeout + resourceCleanupTimeout + sendEmailTimeout
    println("Test duration: $testDuration")
    println("Test run timeout: $testRunTimeout")
    println("Collect logs timeout: $collectLogsTimeout")
    println("Resource cleanup timeout: $resourceCleanupTimeout")
    println("Runner timeout: $runnerTimeout")
    return [testDuration, testRunTimeout, runnerTimeout, collectLogsTimeout, resourceCleanupTimeout]
}
