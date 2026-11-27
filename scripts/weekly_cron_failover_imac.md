# Weekly Hermes cron routing on iMac

Add `weekly_provider_failover` under `cron` in the default profile's existing
`config.yaml` after installing a release containing this change. Keep the
existing primary model and the profile's interactive fallback configuration.

```yaml
cron:
  weekly_provider_failover:
    providers:
      - provider: custom
        model: gpt-6.2-sol
        base_url: http://100.123.205.40:8317/v1
        key_env: CLIPROXY_API_KEY
      - provider: custom
        model: gemini-3.8-flash-high
        base_url: http://100.123.205.40:8317/v1
        key_env: CLIPROXY_API_KEY
    chat_script_job_ids:
      - e03d81f8215d
    failure_command:
      - /opt/homebrew/bin/python3
      - /absolute/path/to/release/scripts/dispatch_weekly_cron_failure.py
      - --sender-thread
      - mcp:launch:<T3 owner thread ID>
```

The models use different upstream providers through the same existing CLIProxy
endpoint. An outage of CLIProxy itself can therefore exhaust the chain. The CTO
dispatcher receives the job and execution IDs. Success uses the existing job
delivery settings. The recap opts in because its script starts its own Hermes
chat subprocess.

Test the existing CLIProxy route with its authenticated `/v1/models` endpoint
and a short tool-free request to each spare model before enabling the policy.
Run `tests/cron/test_weekly_provider_failover.py` against the release to check
429, 503, both fallback steps, and the result of exhausting them. A handoff
failure is recorded in the job's delivery status. Inspect the execution in
`$HERMES_HOME/cron/output/` using the job ID in the dispatcher's register.

Disable the policy by removing the `weekly_provider_failover` mapping. It is
read per run. Installing the code on a currently running gateway requires
that gateway to load the new release.
