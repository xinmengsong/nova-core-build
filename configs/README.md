# Nova build configuration

`nova-kvm.config` is the tested starting configuration for an x86-64 GB202 KVM
host, saved from a working deployment on 2026-10-01. Its SHA-256 is
`f761255c27e1b77cded7d8e7bfc7eca0a31c07e6fd05de06dcb153db699b0bee`.

Set `NOVA_CONFIG` to this file in `nova-build.env`. For a new full-kernel build,
the builder copies it into `<build-dir>/.config`, enables the required
Nova/Rust/VFIO options, replaces the seed's kernel release suffix, and runs
`olddefconfig`. The resulting `.config` belongs to that build; this seed stays
unchanged.

The environment's seed is ignored once the build has a `.config`. Explicit
`--config` is rejected in that case. Module builds reuse a complete baseline
and preserve its configuration and kernel release.

Review storage, network, virtualization, and IOMMU options for a different
host. To customize the seed without changing the shared example:

```bash
cp configs/nova-kvm.config configs/my-host.config
# Review and edit configs/my-host.config for the target host.
```

Run these commands from the repository root, then set this line in your local
`nova-build.env`:

```bash
export NOVA_CONFIG="${NOVA_BUILD_ROOT}/configs/my-host.config"
```

Re-source `nova-build.env` before building. Git ignores `configs/my-host.config`
and other local config copies, while retaining the shared `nova-kvm.config`
and this README. A configuration outside this repository can also be selected
by its absolute path. Use a fresh build directory when you want a changed seed
to initialize a new build.
