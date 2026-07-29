//! Incremental rebuild decisions for the native replay (M6b).
//!
//! Make/SCons-style staleness: a target is rebuilt when it is missing or any of
//! its prerequisites is newer. Compiles use gcc's `-MMD` depfile (emitted next to
//! each object) so a header edit correctly rebuilds every dependent translation
//! unit; the source file is always a prerequisite too. Archives depend on their
//! member objects, the link on all objects+archives, and each image on its inputs.

use std::path::{Path, PathBuf};
use std::time::SystemTime;

use super::plan::{ArchiveAction, CompileAction, ImageAction, LinkAction};

/// Resolve a possibly-relative plan path against the project dir.
fn resolve(project_dir: &Path, p: &str) -> PathBuf {
    let path = Path::new(p);
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        project_dir.join(path)
    }
}

fn mtime(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// True if `target` is missing or any existing prerequisite is strictly newer.
/// A missing prerequisite is ignored (it cannot make the target stale); a missing
/// target always rebuilds.
fn needs_rebuild(target: &Path, prereqs: &[PathBuf]) -> bool {
    let Some(t) = mtime(target) else {
        return true;
    };
    prereqs.iter().any(|p| mtime(p).is_some_and(|m| m > t))
}

/// The depfile path for an object: `foo.cpp.o` → `foo.cpp.d` (gcc `-MMD` default).
fn depfile_for(object: &Path) -> PathBuf {
    object.with_extension("d")
}

/// Parse a Make-style `.d` depfile into prerequisite paths. Handles the
/// `target: a b \` line-continuation form and the `\`-escaped spaces gcc emits.
#[must_use]
pub fn parse_depfile(text: &str) -> Vec<String> {
    // Drop the `target:` prefix (everything up to the first unescaped colon on
    // the first logical line is the target list).
    let joined = text.replace("\\\r\n", " ").replace("\\\n", " ").replace('\\', "/");
    let after_colon = match joined.find(':') {
        Some(i) => &joined[i + 1..],
        None => &joined,
    };
    after_colon
        .split_whitespace()
        .filter(|t| !t.is_empty())
        .map(ToString::to_string)
        .collect()
}

/// Prerequisites of a compile: the source plus every header in its depfile.
fn compile_prereqs(action: &CompileAction, project_dir: &Path) -> Vec<PathBuf> {
    let mut prereqs = vec![resolve(project_dir, &action.source)];
    let depfile = depfile_for(&resolve(project_dir, &action.object));
    if let Ok(text) = std::fs::read_to_string(&depfile) {
        for dep in parse_depfile(&text) {
            prereqs.push(resolve(project_dir, &dep));
        }
    }
    prereqs
}

/// Whether an object must be recompiled.
#[must_use]
pub fn compile_stale(action: &CompileAction, project_dir: &Path) -> bool {
    let object = resolve(project_dir, &action.object);
    needs_rebuild(&object, &compile_prereqs(action, project_dir))
}

/// Whether a static archive must be rebuilt (missing, or any member newer).
#[must_use]
pub fn archive_stale(action: &ArchiveAction, project_dir: &Path) -> bool {
    let archive = resolve(project_dir, &action.archive);
    let members: Vec<PathBuf> = action.objects.iter().map(|o| resolve(project_dir, o)).collect();
    needs_rebuild(&archive, &members)
}

/// Whether the final link must run (elf missing, or any object/archive newer).
#[must_use]
pub fn link_stale(
    link: &LinkAction,
    compiles: &[CompileAction],
    archives: &[ArchiveAction],
    project_dir: &Path,
) -> bool {
    let elf = resolve(project_dir, &link.output);
    let mut prereqs: Vec<PathBuf> =
        compiles.iter().map(|c| resolve(project_dir, &c.object)).collect();
    prereqs.extend(archives.iter().map(|a| resolve(project_dir, &a.archive)));
    needs_rebuild(&elf, &prereqs)
}

/// Whether an image step must run (any output missing, or any input newer).
#[must_use]
pub fn image_stale(action: &ImageAction, project_dir: &Path) -> bool {
    let prereqs: Vec<PathBuf> = action.inputs.iter().map(|i| resolve(project_dir, i)).collect();
    action
        .outputs
        .iter()
        .any(|o| needs_rebuild(&resolve(project_dir, o), &prereqs))
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::plan::Lang;
    use std::fs;

    #[test]
    fn parse_depfile_strips_target_and_splits_prereqs() {
        let d = "build/main.cpp.o: src/main.cpp \\\n  /fw/Arduino.h /fw/esp32-hal.h\n";
        let deps = parse_depfile(d);
        assert_eq!(deps, ["src/main.cpp", "/fw/Arduino.h", "/fw/esp32-hal.h"]);
    }

    #[test]
    fn compile_stale_tracks_source_and_header_mtime() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        fs::create_dir_all(root.join("build")).unwrap();
        fs::create_dir_all(root.join("src")).unwrap();
        let hdr = root.join("h.h");
        fs::write(&hdr, "x").unwrap();
        fs::write(root.join("src/main.cpp"), "y").unwrap();
        // Object + its depfile, both older than the header we bump below.
        fs::write(root.join("build/main.cpp.o"), "obj").unwrap();
        fs::write(root.join("build/main.cpp.d"), "build/main.cpp.o: src/main.cpp h.h\n").unwrap();

        let action = CompileAction {
            command: vec!["g++".into()],
            source: "src/main.cpp".into(),
            object: "build/main.cpp.o".into(),
            lang: Lang::Cpp,
        };
        // Fresh: object is newest.
        // (Touch the object last so it is >= source/header.)
        let now = std::time::SystemTime::now();
        filetime_set(&root.join("build/main.cpp.o"), now + std::time::Duration::from_secs(10));
        assert!(!compile_stale(&action, root), "up-to-date object is not stale");

        // Bump the header past the object -> stale (header dep tracked via depfile).
        filetime_set(&hdr, now + std::time::Duration::from_secs(20));
        assert!(compile_stale(&action, root), "newer header makes it stale");

        // Missing object -> stale.
        fs::remove_file(root.join("build/main.cpp.o")).unwrap();
        assert!(compile_stale(&action, root));
    }

    // Minimal mtime setter for the test (avoids adding the `filetime` crate).
    fn filetime_set(path: &Path, t: SystemTime) {
        let f = fs::OpenOptions::new().write(true).open(path).unwrap();
        f.set_modified(t).unwrap();
    }
}
