# MoviePilot Plugins

This is a general public MoviePilot V3 plugin collection. Each plugin has its own directory, version, documentation and tests. The Go StrmFlow application remains in its separate private repository. Never copy site credentials, NAS configuration, media paths or original HDHive core files into this repository.

- Plugin ID: StrmFlowBridge; directory: plugins.v3/strmflowbridge.
- Keep plugin_version, package.v3.json version and newest history entry consistent.
- Use stable app.sdk interfaces, public Oper/Chain APIs; do not directly edit host models or delete downloads/media.
- Only confirmed successful transfers can create completed movie/episode facts. Partial TV completion never ends the whole subscription.
- Run python3 -m unittest discover -s tests/v3/strmflowbridge and compileall before publishing. These fake-host tests are not a substitute for a real V3-host installation test.
- Plugin settings and durable notification data belong to MoviePilot plugin storage, never source files.
