# Fastlane lane: upload dSYMs to self-hosted Sentry and gate the release on it.
#
#   fastlane add_plugin sentry
#
# Env: SENTRY_URL, SENTRY_AUTH_TOKEN, SENTRY_ORG, SENTRY_PROJECT
# `sentry_debug_files_upload` shells out to sentry-cli, which honours SENTRY_URL.

default_platform(:ios)

platform :ios do
  desc "Build the release archive"
  lane :release_build do
    build_app(
      scheme: ENV.fetch("SCHEME", "MyApp"),
      export_method: "app-store",
      include_symbols: true,
      # Keep dSYMs for every target: app, app extensions, frameworks, pods.
      xcargs: "DEBUG_INFORMATION_FORMAT=dwarf-with-dsym"
    )
  end

  desc "Upload every dSYM in the archive to Sentry and fail if the app binary is not covered"
  lane :upload_symbols do
    dsym_dir = File.join(lane_context[SharedValues::XCODEBUILD_ARCHIVE], "dSYMs")
    UI.user_error!("No dSYMs found at #{dsym_dir}") unless Dir.exist?(dsym_dir)

    sentry_debug_files_upload(
      path: dsym_dir,
      include_sources: true,   # source context in the stack trace
      wait: true,              # block until Sentry finished processing
      org_slug: ENV.fetch("SENTRY_ORG"),
      project_slug: ENV.fetch("SENTRY_PROJECT")
    )

    # Gate: every DWARF the app binary ships must now exist in Sentry.
    app_dsym = Dir.glob(File.join(dsym_dir, "*.app.dSYM")).first
    UI.user_error!("App dSYM missing in #{dsym_dir}") unless app_dsym
    sh("sentry-cli", "debug-files", "check",
       "--org", ENV.fetch("SENTRY_ORG"), "--project", ENV.fetch("SENTRY_PROJECT"),
       app_dsym)
  end

  desc "Create the Sentry release and associate commits (optional, enables suspect commits)"
  lane :sentry_release do
    version = get_version_number(target: ENV.fetch("SCHEME", "MyApp"))
    build = get_build_number
    sentry_create_release(
      app_identifier: ENV.fetch("BUNDLE_ID"),
      version: version,
      build: build,
      finalize: true
    )
    sentry_set_commits(
      app_identifier: ENV.fetch("BUNDLE_ID"),
      version: version,
      build: build,
      auto: true
    )
  end

  lane :release do
    release_build
    upload_symbols
    sentry_release
    upload_to_testflight
  end
end
