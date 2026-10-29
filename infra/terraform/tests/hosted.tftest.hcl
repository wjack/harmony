# `terraform test` (make tf-check): the hosted env and the passcode rules, against mock
# providers. Nothing reaches Render or Temporal Cloud, and random_password runs locally.
#
# Every variable an assertion depends on is set here, because a local terraform.tfvars
# is loaded too and these values must win over it.

mock_provider "render" {}
mock_provider "temporalcloud" {}

variables {
  anthropic_api_key        = "sk-ant-test-only"
  model_override           = "claude-haiku-5-5"
  access_code              = null
  client_ip_header         = "true-client-ip"
  worker_model_concurrency = 13
  worker_model_rate        = 2.5
  worker_chat_concurrency  = 5
}

run "both_services_get_the_live_settings" {
  command = apply

  assert {
    condition     = local.app_env.APP_ENV == "prod" && local.app_env.MODEL_MODE == "live"
    error_message = "Both services must run with APP_ENV=prod and MODEL_MODE=live."
  }

  assert {
    condition     = local.app_env.MODEL_OVERRIDE == "claude-haiku-5-5"
    error_message = "Both services must get MODEL_OVERRIDE from var.model_override."
  }

  assert {
    condition     = local.app_env.ACCESS_CODE == local.access_code
    error_message = "ACCESS_CODE must be local.access_code."
  }

  assert {
    condition     = local.app_env.CLIENT_IP_HEADER == "true-client-ip"
    error_message = "Both services must get CLIENT_IP_HEADER."
  }

  assert {
    condition     = !contains(keys(local.app_env), "ANTHROPIC_API_KEY")
    error_message = "The model key must not reach the web service."
  }

  assert {
    condition     = local.worker_env.ANTHROPIC_API_KEY == "sk-ant-test-only"
    error_message = "The worker must get the model key."
  }

  assert {
    condition = (
      local.worker_env.WORKER_MODEL_CONCURRENCY == "13"
      && local.worker_env.WORKER_MODEL_RATE == "2.5"
      && local.worker_env.WORKER_CHAT_CONCURRENCY == "5"
    )
    error_message = "The worker must get its capacity settings."
  }
}

run "the_generated_passcode_is_16_characters" {
  command = apply

  assert {
    condition     = random_password.access_code.length == 16
    error_message = "random_password.access_code must be 16 characters."
  }

  assert {
    condition     = local.access_code == random_password.access_code.result
    error_message = "With no access_code set, the generated passcode is used."
  }
}

run "a_chosen_passcode_wins" {
  command = plan

  variables {
    access_code = "correct horse battery staple"
  }

  assert {
    condition     = local.access_code == "correct horse battery staple"
    error_message = "A passcode set in terraform.tfvars must win over the generated one."
  }
}

run "a_16_character_passcode_passes" {
  command = plan

  variables {
    access_code = "sixteen-chars-xy"
  }

  assert {
    condition     = local.access_code == "sixteen-chars-xy"
    error_message = "A 16-character passcode must pass."
  }
}

run "a_15_character_passcode_is_refused" {
  command = plan

  variables {
    access_code = "fifteen-chars-x"
  }

  expect_failures = [var.access_code]
}

run "an_unknown_model_override_is_refused" {
  command = plan

  variables {
    model_override = "claude-haiku-5"
  }

  expect_failures = [var.model_override]
}

run "a_passcode_with_a_leading_space_is_refused" {
  command = plan

  variables {
    access_code = " correct horse battery staple"
  }

  expect_failures = [var.access_code]
}

run "a_passcode_with_a_trailing_newline_is_refused" {
  command = plan

  variables {
    access_code = "correct horse battery staple\n"
  }

  expect_failures = [var.access_code]
}

run "a_non_ascii_passcode_is_refused" {
  command = plan

  variables {
    access_code = "correct horse battery stäple"
  }

  expect_failures = [var.access_code]
}

# The worker's settings read the slot counts as integers, so a fraction would fail every
# worker deploy after a clean apply.
run "a_fractional_model_concurrency_is_refused" {
  command = plan

  variables {
    worker_model_concurrency = 12.5
  }

  expect_failures = [var.worker_model_concurrency]
}

run "a_fractional_chat_concurrency_is_refused" {
  command = plan

  variables {
    worker_chat_concurrency = 2.5
  }

  expect_failures = [var.worker_chat_concurrency]
}

run "a_development_key_shape_is_refused" {
  command = plan

  variables {
    anthropic_api_key = "not-an-anthropic-key"
  }

  expect_failures = [var.anthropic_api_key]
}

# The module puts the env on each service itself: no env group, the database URL on
# both, and the worker-only keys on the worker alone.
run "each_service_carries_its_own_env" {
  command = apply

  module {
    source = "./modules/render"
  }

  variables {
    region                = "oregon"
    environment_id        = "evm-test"
    repo_url              = "https://github.com/wjack/harmony"
    branch                = "main"
    web_plan              = "starter"
    worker_plan           = "standard"
    postgres_disk_size_gb = 5
    app_env               = { MODEL_MODE = "live", ACCESS_CODE = "correct horse battery staple" }
    worker_env            = { ANTHROPIC_API_KEY = "sk-ant-test-only" }
  }

  assert {
    condition = alltrue([
      for env in [render_web_service.web.env_vars, render_background_worker.worker.env_vars] :
      env.MODEL_MODE.value == "live"
      && env.ACCESS_CODE.value == "correct horse battery staple"
      && contains(keys(env), "DATABASE_URL")
      && contains(keys(env), "DATABASE_DIRECT_URL")
    ])
    error_message = "Both services must carry the shared env and the database URL."
  }

  assert {
    condition     = !contains(keys(render_web_service.web.env_vars), "ANTHROPIC_API_KEY")
    error_message = "The web service must not get the model key."
  }

  assert {
    condition     = render_background_worker.worker.env_vars.ANTHROPIC_API_KEY.value == "sk-ant-test-only"
    error_message = "The worker must get the model key."
  }
}

# Section 6's deploy contract: both services build main's Dockerfile and deploy only
# once CI is green, migrate before they start, and the web service runs one instance
# (the gate's limiters are in-process) behind /api/healthz.
run "services_deploy_main_after_checks" {
  command = apply

  module {
    source = "./modules/render"
  }

  variables {
    region                = "oregon"
    environment_id        = "evm-test"
    repo_url              = "https://github.com/wjack/harmony"
    branch                = "main"
    web_plan              = "starter"
    worker_plan           = "standard"
    postgres_disk_size_gb = 5
    app_env               = { MODEL_MODE = "live" }
    worker_env            = { ANTHROPIC_API_KEY = "sk-ant-test-only" }
  }

  assert {
    condition = alltrue([
      for docker in [
        render_web_service.web.runtime_source.docker,
        render_background_worker.worker.runtime_source.docker,
      ] :
      docker.auto_deploy_trigger == "checksPass"
      && docker.branch == "main"
      && docker.dockerfile_path == "./Dockerfile"
      && contains(docker.build_filter.ignored_paths, "infra/**")
    ])
    error_message = "Both services must build main's Dockerfile and deploy only after CI passes."
  }

  assert {
    condition = (
      render_web_service.web.pre_deploy_command == "alembic upgrade head"
      && render_background_worker.worker.pre_deploy_command == "alembic upgrade head"
    )
    error_message = "Both services must migrate before they start."
  }

  assert {
    condition = (
      render_web_service.web.health_check_path == "/api/healthz"
      && render_web_service.web.num_instances == 1
      && render_web_service.web.start_command == null
    )
    error_message = "The web service runs the image's CMD on one instance, health-checked at /api/healthz."
  }

  assert {
    condition     = render_background_worker.worker.start_command == "harmony worker"
    error_message = "The worker must run `harmony worker`."
  }
}

run "minio_keeps_its_data_on_the_disk" {
  command = apply

  module {
    source = "./modules/storage"
  }

  variables {
    region         = "oregon"
    environment_id = "evm-test"
    repo_url       = "https://github.com/wjack/harmony"
    branch         = "main"
    plan           = "starter"
    disk_size_gb   = 10
    root_user      = "harmony"
    root_password  = "test-only-password"
    bucket         = "harmony"
  }

  assert {
    condition     = render_private_service.minio.disk.mount_path == "/data"
    error_message = "MinIO's disk must be mounted where the image's CMD serves from (/data)."
  }

  assert {
    condition     = endswith(output.endpoint_url, ":9000") && startswith(output.endpoint_url, "http://")
    error_message = "S3_ENDPOINT_URL must be http://<slug>:9000, MinIO's --address."
  }

  assert {
    condition     = render_private_service.minio.runtime_source.docker.auto_deploy_trigger == "checksPass"
    error_message = "MinIO must deploy only after CI passes."
  }
}
