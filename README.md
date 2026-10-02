# nova-core-build

Build [Zhi's Nova kernel tree](https://github.com/zhiwang-nvidia/nova-core),
including `nvidia-vgpu-vfio-pci`, and package a full kernel or a VF module with
checksums and build provenance. This project contains the build tooling; keep
the kernel source in a separate checkout. The supported source layout is
`zhi/nova-vgpu-wip`, with the Rust VF driver under
`drivers/vfio/pci/nvidia-vgpu/`.

## Requirements

The current workflow builds an x86-64 kernel on an x86-64 Linux machine. It
uses the x86 `bzImage` target and has no architecture-selection option.
Install Python 3.9 or newer, Git, make, the host development tools and libraries
required by your kernel configuration, `readelf`, `modinfo`, and the selected
compression tools. LLVM, Rust, bindgen, and matching Rust library sources must
also be installed. The tested versions and compiler archive checksum are
recorded in [toolchain.json](toolchain.json).

## Configure your own workspace

Clone this repository and run the following from its root:

```bash
cp -n nova-build.env.example nova-build.env
# Open nova-build.env in your editor and review the settings below.
```

The tracked [nova-build.env.example](nova-build.env.example) is the shared
template. **Put your machine-specific settings in `nova-build.env`, which Git
ignores.** The template derives `NOVA_BUILD_ROOT` from its own location and
contains no personal workspace paths. Copying it does not start a build.

Review these settings in your local copy:

| Setting | What to select on your machine |
|---|---|
| `NOVA_SOURCE` | Your Nova kernel checkout; the default is the sibling `../nova-core`. |
| `NOVA_BUILD_DIR` | A persistent output directory; the default is `build/` in this repository. |
| `NOVA_CONFIG` | A kernel config suitable for the target host; defaults to the supplied seed. |
| `NOVA_TOOLCHAIN` | Your installed LLVM/Rust directory, or the default installed in the next section. |
| `NOVA_PYTHON` | An installed Python 3.9 or newer; the example selects `python3.13`. |
| `NOVA_JOBS` | The number of parallel build jobs appropriate for your machine; the example uses 20. |

If `NOVA_SOURCE` does not exist, the example's `NOVA_CLONE_REF` makes the first
build clone `zhi/nova-vgpu-wip`. An existing checkout is used at its current
revision; the script does not switch its branch or apply patches. For
repeatable builds, choose a commit SHA for a new clone or check out the desired
commit yourself. The resolved source commit is recorded in every bundle.

Review [configs/nova-kvm.config](configs/nova-kvm.config) for the target host,
especially storage, networking, KVM, and IOMMU drivers. It is a tested x86-64
GB202 KVM seed. To customize it, copy it to the ignored `configs/my-host.config`
and set `NOVA_CONFIG="${NOVA_BUILD_ROOT}/configs/my-host.config"` in your local
environment file. See [configs/README.md](configs/README.md) for the commands.

After editing, load your settings in Bash:

```bash
source ./nova-build.env
```

Re-source the file whenever you edit it or start a new shell. Keep the same
source, toolchain, and build-directory paths for module iteration against an
already deployed kernel.

## Install the toolchain

The build script uses the compiler's `bin/` and `lib/libclang.so` when
`NOVA_TOOLCHAIN` is set, and checks `make LLVM=1 rustavailable` before compiling.
It does not install build dependencies. For an existing toolchain installation,
set its directory in your local environment file. To install the recorded
compiler archive at the template's default location, run from the repository
root (requires `curl`, `tar`, `xz`, and `sha256sum`):

```bash
mkdir -p downloads toolchains
curl --fail --location \
  https://kernel.org/pub/tools/llvm/rust/files/llvm-22.1.8-rust-1.97.0-x86_64.tar.xz \
  --output downloads/llvm-22.1.8-rust-1.97.0-x86_64.tar.xz
printf '%s  %s\n' \
  16b7ab0c416561942ae6bb2a8dc9360b8958f1a082aa05c1ab7dfdc8e12c80c1 \
  downloads/llvm-22.1.8-rust-1.97.0-x86_64.tar.xz | sha256sum --check - && \
  tar -xJf downloads/llvm-22.1.8-rust-1.97.0-x86_64.tar.xz -C toolchains
```

If the selected toolchain does not yet contain bindgen, install the recorded
version with its Cargo, using the settings sourced above:

```bash
PATH="$NOVA_TOOLCHAIN/bin:$PATH" \
LIBCLANG_PATH="$NOVA_TOOLCHAIN/lib/libclang.so" \
  "$NOVA_TOOLCHAIN/bin/cargo" install --locked bindgen-cli --version 0.72.1 \
  --root "$NOVA_TOOLCHAIN"
```

## Build a complete kernel

```bash
source ./nova-build.env
./build-nova.py
```

Direct execution selects `NOVA_PYTHON` before loading the build implementation,
including on machines whose default `python3` is Python 3.6. The selected
interpreter is also used by Kbuild. The script reads exported settings; it
does not automatically source an environment file.

`NOVA_BUILD_DIR` is the persistent Kbuild output directory. The example uses
`build/`; local settings may select another directory. A new
full-kernel build copies `NOVA_CONFIG` into `<build-dir>/.config`, enables the
Nova/Rust/VFIO options, chooses a kernel release suffix, and runs `olddefconfig`.
Subsequent builds reuse that `.config`. The seed file is preserved.

`NOVA_CONFIG` names an input file; `NOVA_BUILD_DIR` names an output directory.
With the example settings, the configuration flows from
`configs/nova-kvm.config` into `build/.config`. Changes to the seed are not
automatically reapplied to an existing working `.config`.

Changing `NOVA_BUILD_DIR` selects a different build location. Complete a full
kernel build and deploy it before using module mode in a new directory;
retain the original directory when iterating against an older deployed kernel.

Command-line options override environment variables, which override defaults:

| Variable | Option | Meaning / default |
|---|---|---|
| `NOVA_SOURCE` | `--source` | Required kernel checkout or new clone destination. |
| `NOVA_BUILD_DIR` | `--build-dir` | Required persistent output directory, separate from source. |
| `NOVA_CONFIG` | `--config` | Seed for a new full-kernel build only. |
| `NOVA_TOOLCHAIN` | `--toolchain` | Directory with `bin/` and `lib/`; empty uses `PATH`. |
| `NOVA_PYTHON` | `--python` | Script and Kbuild interpreter; default is the current interpreter. |
| `NOVA_JOBS` | `--jobs` | Positive parallel job count; default CPU count capped at 24. |
| `NOVA_KIND` | `--kind` | `kernel` (default) or `module`. |
| `NOVA_ALLOW_DIRTY` | `--allow-dirty` / `--no-allow-dirty` | `0` (default) or `1` to allow tracked edits. |
| `NOVA_TAG` | `--nova-tag` | Bundle label; empty generates UTC `YYYYMMDD-HHMMSS`. |
| `NOVA_OUTPUT` | `--output` | Bundle path; empty uses `<build-dir>/artifacts/`. |
| `NOVA_LOCALVERSION` | `--localversion` | Full-kernel release suffix; empty generates a commit/time suffix. |
| `NOVA_CLONE_REF` | `--clone-ref` | Initial clone revision; environment value is ignored for an existing checkout. |

`NOVA_BUILD_ROOT` groups paths in the environment file. It is not a build-script
option. The example derives it from the environment file's location, so it can
be sourced from any working directory. Sourcing reapplies the saved values;
make one-off exports after sourcing.

`NOVA_TAG` labels the artifact; it does not change the build directory, source
revision, or kernel release. A tag must be 1–64 ASCII letters, digits, dots,
`+`, `-`, or `_`, beginning with a letter or digit. Existing artifacts and
sidecars are never overwritten. Use a new tag or output path for each build.

Full-kernel builds choose a new kernel release by default. To retain a chosen
release during iteration, set `NOVA_LOCALVERSION` or pass, for example,
`--localversion=-nova-test-1`. Explicit `--config` is rejected if the build
directory already contains `.config`; omit that option to reuse it.

By default the source must be clean. `--allow-dirty` records tracked edits in
the bundle; untracked source files must first be added to Git. Keep the source
and output directory dedicated to the build while it runs.

## VF module iteration

First build and deploy a complete kernel. Then use its original build directory
and the same compiler, configuration, and Kbuild Python setting:

```bash
source ./nova-build.env
./build-nova.py --kind module
```

Module mode requires the existing `.config`, `Module.symvers`, `vmlinux`, and
`bzImage`. It preserves the configuration and kernel release, runs an
incremental kernel/module build, and packages only `nvidia-vgpu-vfio-pci.ko`.
It rejects a changed built-in kernel build ID. The bundle also records the
Nova PF, VFIO core, and VFIO module build IDs for deployment compatibility
checks. A matching `uname -r` alone is insufficient.

Preserve the original build directory's location. Rebuilding after moving it
can change build IDs. Changes to built-in code or PF/VFIO dependencies require
a full kernel deployment when those IDs no longer match the running host.

## Artifacts and local files

Default bundles are `<build-dir>/artifacts/nova-<tag>.tar.gz` for a kernel and
`nova-vf-<tag>.tar.gz` for a VF module. Each has a `.json` manifest sidecar and a
`.build.log`. The bundle records its source commit/diff, kernel config,
`Module.symvers`, compiler versions, file checksums, vermagic, and build IDs.
It retains format version 1 for the existing Nova deployment tools.

This repository's root uses an explicit Git allowlist because it can also
contain existing build workspaces. Local `nova-build.env`, output directories,
toolchains, downloads, artifacts, and historical build records are ignored.
Add new project paths to `.gitignore` when extending the tooling. Kernel
sources, deployment tools, host settings, and firmware are managed separately.

## Files to publish

Commit the scripts, documentation, tests, `.gitignore`, `toolchain.json`, the
shared config seed, and `nova-build.env.example`. Each contributor creates
their own ignored `nova-build.env` and optional `configs/my-host.config`.
Build bundles and manifests include local build metadata and stay with the
ignored outputs. Custom output-directory names at the repository root are
also ignored.

Review what Git would include before committing or pushing:

```bash
git status --short --untracked-files=all
git add --dry-run .
git check-ignore nova-build.env build/.config configs/my-host.config
```

The first two commands should list only shared project files. The last command
should print all three local paths, confirming that the ignore rules cover
them even before those files exist.

## Checks

```bash
python3.13 -m unittest discover -s tests -v
```

The tests exercise configuration, Python selection, portable environment
settings, and bundle verification without compiling a kernel or accessing a
host. Use any installed Python 3.9 or newer to run them.
