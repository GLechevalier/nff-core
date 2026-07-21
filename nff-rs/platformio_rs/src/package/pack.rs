//! Port of `platformio/package/pack.py` (`PackagePacker`) plus the slice of
//! `platformio/fs.py` it needs (`match_src_files`, the `+<pat>`/`-<pat>` glob
//! filter engine).
//!
//! The behavioural spec is `tests/package/test_pack.py`, mirrored inline below.
//! Packing *from an archive* (vs a directory) is only reachable on non-Windows
//! upstream; we reproduce the Windows guard and do a best-effort extract
//! elsewhere.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

use glob::MatchOptions;
use regex::Regex;
use serde_json::Value;

use crate::package::error::{PackageError, Result};
use crate::package::manifest::parser::{collect_dirs_topdown, ManifestFileType, ManifestParserFactory};
use crate::package::manifest::schema::ManifestSchema;

const INCLUDE_DEFAULT: &[&str] = &[
    "platform.json",
    "library.json",
    "library.properties",
    "module.json",
    "package.json",
    "README",
    "README.md",
    "README.rst",
    "LICENSE",
];

const EXCLUDE_DEFAULT: &[&str] = &[
    ".piopm",
    ".pio/",
    "**/.pio/",
    "._*",
    "__*",
    ".DS_Store",
    ".vscode",
    "**/.vscode/",
    ".cache",
    "**/.cache",
    "**/__pycache__",
    "**/*.pyc",
    ".git/",
    ".hg/",
    ".svn/",
];

const EXCLUDE_EXTRA: &[&str] = &[
    "test",
    "tests",
    "doc",
    "docs",
    "mkdocs",
    "doxygen",
    "*.doxyfile",
    "html",
    "media",
    "**/*.[pP][dD][fF]",
    "**/*.[dD][oO][cC]",
    "**/*.[dD][oO][cC][xX]",
    "**/*.[pP][pP][tT]",
    "**/*.[pP][pP][tT][xX]",
    "**/*.[xX][lL][sS]",
    "**/*.[xX][lL][sS][xX]",
    "**/*.[dD][oO][xX]",
    "**/*.[hH][tT][mM]",
    "**/*.[hH][tT][mM][lL]",
    "**/*.[tT][eE][xX]",
    "**/*.[jJ][sS]",
    "**/*.[cC][sS][sS]",
    "**/*.[jJ][pP][gG]",
    "**/*.[jJ][pP][eE][gG]",
    "**/*.[pP][nN][gG]",
    "**/*.[gG][iI][fF]",
    "**/*.[sS][vV][gG]",
    "**/*.[zZ][iI][pP]",
    "**/*.[gG][zZ]",
    "**/*.3[gG][pP]",
    "**/*.[mM][oO][vV]",
    "**/*.[mM][pP][34]",
    "**/*.[pP][sS][dD]",
    "**/*.[wW][aA][wW]",
    "**/*.sqlite",
];

const EXCLUDE_LIBRARY_EXTRA: &[&str] = &[
    "assets",
    "extra",
    "extras",
    "resources",
    "**/build/",
    "**/*.flat",
    "**/*.[jJ][aA][rR]",
    "**/*.[eE][xX][eE]",
    "**/*.[bB][iI][nN]",
    "**/*.[hH][eE][xX]",
    "**/*.[dD][bB]",
    "**/*.[dD][aA][tT]",
    "**/*.[dD][lL][lL]",
];

/// `platformio.package.pack.PackagePacker`.
pub struct PackagePacker {
    package: PathBuf,
    manifest_uri: Option<String>,
}

impl PackagePacker {
    #[must_use]
    pub fn new(package: impl Into<PathBuf>, manifest_uri: Option<String>) -> Self {
        Self { package: package.into(), manifest_uri }
    }

    /// `PackagePacker.get_archive_name`.
    #[must_use]
    pub fn get_archive_name(name: &str, version: &str, system: Option<&str>) -> String {
        static RE: OnceLock<Regex> = OnceLock::new();
        let re = RE.get_or_init(|| Regex::new(r"[^0-9a-zA-Z\-._+]+").unwrap());
        let system = system.map_or_else(String::new, |s| format!("-{s}"));
        re.replace_all(&format!("{name}{system}-{version}.tar.gz"), "").into_owned()
    }

    /// `PackagePacker.load_gitignore_filters`.
    fn load_gitignore_filters(path: &Path) -> Vec<String> {
        let mut result = Vec::new();
        let Ok(text) = fs::read_to_string(path) else { return result };
        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            if let Some(rest) = line.strip_prefix('!') {
                result.push(format!("+<{rest}>"));
            } else {
                result.push(format!("-<{line}>"));
            }
        }
        result
    }

    /// `PackagePacker.pack`.
    pub fn pack(&self, dst: Option<&Path>) -> Result<PathBuf> {
        let mut src = self.package.clone();
        if !src.is_dir() {
            if cfg!(windows) {
                return Err(PackageError::Package {
                    message: format!(
                        "Packaging from an archive does not work on Windows OS. Please \
                         extract data from `{}` manually and pack a folder instead",
                        src.display()
                    ),
                });
            }
            // Best-effort extract (untested on this platform).
            let tmp = std::env::temp_dir().join(format!("pio-pack-{}", src.file_name().and_then(|s| s.to_str()).unwrap_or("pkg")));
            let _ = fs::remove_dir_all(&tmp);
            fs::create_dir_all(&tmp).ok();
            extract_targz(&src, &tmp)?;
            src = tmp;
        }

        src = self.find_source_root(&src)?;
        let manifest_type = ManifestFileType::from_dir(&src);
        let manifest_value = ManifestParserFactory::new_from_dir(&src, None)?.as_dict();
        let manifest = ManifestSchema::new().load_manifest(&manifest_value)?;

        let name = manifest.get("name").and_then(Value::as_str).unwrap_or("");
        let version = manifest.get("version").and_then(Value::as_str).unwrap_or("");
        let system =
            manifest.get("system").and_then(Value::as_array).and_then(|a| a.first()).and_then(Value::as_str);
        let filename = Self::get_archive_name(name, version, system);

        let dst = match dst {
            None => std::env::current_dir()
                .map_err(|e| PackageError::Package { message: e.to_string() })?
                .join(&filename),
            Some(d) if d.is_dir() => d.join(&filename),
            Some(d) => d.to_path_buf(),
        };

        self.create_tarball(&src, &dst, &manifest, manifest_type)
    }

    /// `PackagePacker.find_source_root`.
    fn find_source_root(&self, src: &Path) -> Result<PathBuf> {
        if let Some(uri) = &self.manifest_uri {
            let mp = if let Some(path) = uri.strip_prefix("file:") {
                ManifestParserFactory::new_from_file(Path::new(path), None)?
            } else {
                return Err(PackageError::Package {
                    message: "remote manifest_uri fetch is not supported yet".to_string(),
                });
            };
            let manifest = ManifestSchema::new().load_manifest(&mp.as_dict())?;
            let include = manifest
                .get("export")
                .and_then(|e| e.get("include"))
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default();
            if include.len() == 1 {
                let inc = include[0].as_str().unwrap_or("");
                if !src.join(inc).is_dir() {
                    return Err(PackageError::Package {
                        message: format!("Non existing `include` directory `{inc}` in a package"),
                    });
                }
                return Ok(src.join(inc));
            }
        }
        let mut dirs = Vec::new();
        collect_dirs_topdown(src, &mut dirs);
        for root in dirs {
            if ManifestFileType::from_dir(&root).is_some() {
                return Ok(root);
            }
        }
        Ok(src.to_path_buf())
    }

    /// `PackagePacker.create_tarball`.
    fn create_tarball(
        &self,
        src: &Path,
        dst: &Path,
        manifest: &Value,
        manifest_type: Option<&'static str>,
    ) -> Result<PathBuf> {
        let export = manifest.get("export");
        let mut include: Option<Vec<String>> = export
            .and_then(|e| e.get("include"))
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect());
        let exclude: Option<Vec<String>> = export
            .and_then(|e| e.get("exclude"))
            .and_then(Value::as_array)
            .map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect());

        // Remap root: a single `include` dir becomes the new source root.
        let mut src = src.to_path_buf();
        if let Some(inc) = &include {
            if inc.len() == 1 && src.join(&inc[0]).is_dir() {
                src = src.join(&inc[0]);
                // Write a library.json with `export.include` stripped.
                let mut updated = manifest.clone();
                if let Some(exp) = updated.get_mut("export").and_then(Value::as_object_mut) {
                    exp.remove("include");
                }
                let text = serde_json::to_string_pretty(&updated)
                    .map_err(|e| PackageError::Package { message: e.to_string() })?;
                fs::write(src.join("library.json"), text)
                    .map_err(|e| PackageError::Package { message: e.to_string() })?;
                include = None;
            }
        }

        let filters = compute_src_filters(&src, include.as_deref(), exclude.as_deref(), manifest_type == Some(ManifestFileType::LIBRARY_PROPERTIES));
        let files = match_src_files(&src, &filters);

        let out = fs::File::create(dst).map_err(|e| PackageError::Package { message: e.to_string() })?;
        let enc = flate2::write::GzEncoder::new(out, flate2::Compression::default());
        let mut tar = tar::Builder::new(enc);
        for f in &files {
            tar.append_path_with_name(src.join(f), f)
                .map_err(|e| PackageError::Package { message: e.to_string() })?;
        }
        tar.into_inner()
            .and_then(flate2::write::GzEncoder::finish)
            .map_err(|e| PackageError::Package { message: e.to_string() })?;
        Ok(dst.to_path_buf())
    }
}

/// `PackagePacker.compute_src_filters`.
fn compute_src_filters(src: &Path, include: Option<&[String]>, exclude: Option<&[String]>, is_library_properties: bool) -> Vec<String> {
    let mut exclude_extra: Vec<&str> = EXCLUDE_EXTRA.to_vec();
    let has_library_manifest = [
        ManifestFileType::LIBRARY_JSON,
        ManifestFileType::LIBRARY_PROPERTIES,
        ManifestFileType::MODULE_JSON,
    ]
    .iter()
    .any(|n| src.join(n).is_file());
    if has_library_manifest {
        exclude_extra.extend_from_slice(EXCLUDE_LIBRARY_EXTRA);
    }

    let include_empty = include.is_none_or(<[String]>::is_empty);
    let exclude_empty = exclude.is_none_or(<[String]>::is_empty);

    let mut result: Vec<String> = Vec::new();
    let default_include = [String::from("*"), String::from(".*")];
    let inc = if include_empty { &default_include[..] } else { include.unwrap() };
    for p in inc {
        result.push(format!("+<{p}>"));
    }
    for p in EXCLUDE_DEFAULT {
        result.push(format!("-<{p}>"));
    }
    for p in exclude.unwrap_or(&[]) {
        result.push(format!("-<{p}>"));
    }
    if (include_empty && exclude_empty) || is_library_properties {
        for p in &exclude_extra {
            result.push(format!("-<{p}>"));
        }
        let gi = src.join(".gitignore");
        if gi.exists() {
            result.extend(PackagePacker::load_gitignore_filters(&gi));
        }
    }
    for p in INCLUDE_DEFAULT {
        result.push(format!("+<{p}>"));
    }
    result
}

const GLOB_OPTS: MatchOptions = MatchOptions {
    case_sensitive: true,
    require_literal_separator: true,
    require_literal_leading_dot: true,
};

/// `platformio.fs.match_src_files` — apply `+<pat>`/`-<pat>` in order.
///
/// Rather than run the `glob` crate over the filesystem (which mishandles
/// absolute Windows path prefixes), we enumerate every entry under `src_dir`
/// once and match each `/`-relative path against the pattern.
fn match_src_files(src_dir: &Path, filters: &[String]) -> Vec<String> {
    static RE: OnceLock<Regex> = OnceLock::new();
    let re = RE.get_or_init(|| Regex::new(r"(\+|\-)<([^>]+)>").unwrap());
    let entries = list_entries(src_dir);
    let mut result: std::collections::BTreeSet<String> = std::collections::BTreeSet::new();
    for filter in filters {
        for cap in re.captures_iter(filter) {
            let action = &cap[1];
            let candidates = find_candidates(&entries, &cap[2]);
            if action == "+" {
                result.extend(candidates);
            } else {
                for c in candidates {
                    result.remove(&c);
                }
            }
        }
    }
    result.into_iter().collect()
}

/// Match `pattern` against the entry list, expanding matched directories to all
/// files beneath them (mirrors `glob.glob` + the `os.walk` on directory hits).
fn find_candidates(entries: &[(String, bool)], pattern: &str) -> Vec<String> {
    let pat = match glob::Pattern::new(pattern.trim_end_matches('/')) {
        Ok(p) => p,
        Err(_) => return Vec::new(),
    };
    let mut out = Vec::new();
    for (rel, is_dir) in entries {
        if !pat.matches_with(rel, GLOB_OPTS) {
            continue;
        }
        if *is_dir {
            let prefix = format!("{rel}/");
            for (r, d) in entries {
                if !d && r.starts_with(&prefix) {
                    out.push(r.clone());
                }
            }
        } else {
            out.push(rel.clone());
        }
    }
    out
}

/// Every entry under `src_dir` as `(relative-path-with-slashes, is_dir)`.
fn list_entries(src_dir: &Path) -> Vec<(String, bool)> {
    let mut out = Vec::new();
    walk_entries(src_dir, src_dir, &mut out);
    out
}

fn walk_entries(dir: &Path, base: &Path, out: &mut Vec<(String, bool)>) {
    let Ok(entries) = fs::read_dir(dir) else { return };
    for e in entries.filter_map(std::result::Result::ok) {
        let path = e.path();
        let is_dir = path.is_dir();
        let rel = path
            .strip_prefix(base)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_default();
        if rel.is_empty() {
            continue;
        }
        out.push((rel, is_dir));
        if is_dir {
            walk_entries(&path, base, out);
        }
    }
}

/// Best-effort `tar.gz` extraction (used only for archive input on non-Windows).
fn extract_targz(archive: &Path, dst: &Path) -> Result<()> {
    let file = fs::File::open(archive).map_err(|e| PackageError::Package { message: e.to_string() })?;
    let mut ar = tar::Archive::new(flate2::read::GzDecoder::new(file));
    ar.unpack(dst).map_err(|e| PackageError::Package { message: e.to_string() })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn write(root: &Path, rel: &str, contents: &str) {
        let p = root.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, contents).unwrap();
    }

    /// Names inside a produced `tar.gz`, as a set with `/` separators.
    fn tar_names(path: &Path) -> BTreeSet<String> {
        let file = fs::File::open(path).unwrap();
        let mut ar = tar::Archive::new(flate2::read::GzDecoder::new(file));
        ar.entries()
            .unwrap()
            .filter_map(std::result::Result::ok)
            .map(|e| e.path().unwrap().to_string_lossy().replace('\\', "/"))
            .collect()
    }

    fn set(items: &[&str]) -> BTreeSet<String> {
        items.iter().map(|s| (*s).to_string()).collect()
    }

    #[test]
    fn test_base() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, ".git/file", "");
        write(root, ".gitignore", "");
        write(root, "._hidden_file", "");
        write(root, "main.cpp", "#include <stdio.h>");
        let p = PackagePacker::new(root, None);
        // missing manifest
        assert!(p.pack(Some(root)).is_err());
        // minimal package
        write(root, "library.json", r#"{"name": "foo", "version": "1.0.0"}"#);
        write(root, "include/main.h", "#ifndef");
        p.pack(Some(root)).unwrap();
        assert_eq!(
            tar_names(&root.join("foo-1.0.0.tar.gz")),
            set(&[".gitignore", "include/main.h", "library.json", "main.cpp"])
        );
    }

    #[test]
    fn test_filters() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, "src/main.cpp", "#include <stdio.h>");
        write(root, "src/util/helpers.cpp", "void");
        write(root, "include/main.h", "#ifndef");
        write(root, "tests/test_1.h", "");
        write(root, "tests/test_2.h", "");

        // include with remap of root
        write(root, "library.json", r#"{"name": "bar", "version": "1.2.3", "export": {"include": "src"}}"#);
        let out = PackagePacker::new(root, None).pack(Some(root)).unwrap();
        assert_eq!(tar_names(&out), set(&["util/helpers.cpp", "main.cpp", "library.json"]));
        let _ = fs::remove_file(root.join("src/library.json"));

        // include "src" and "include"
        write(root, "library.json", r#"{"name": "bar", "version": "1.2.3", "export": {"include": ["src", "include"]}}"#);
        let out = PackagePacker::new(root, None).pack(Some(root)).unwrap();
        assert_eq!(
            tar_names(&out),
            set(&["include/main.h", "library.json", "src/main.cpp", "src/util/helpers.cpp"])
        );

        // include & exclude
        write(root, "library.json", r#"{"name": "bar", "version": "1.2.3", "export": {"include": ["src", "include"], "exclude": ["*/*.h"]}}"#);
        let out = PackagePacker::new(root, None).pack(Some(root)).unwrap();
        assert_eq!(tar_names(&out), set(&["library.json", "src/main.cpp", "src/util/helpers.cpp"]));
    }

    #[test]
    fn test_gitignore_filters() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, ".git/file", "");
        write(
            root,
            ".gitignore",
            "\n# comment\n\ngi_file\ngi_folder\ngi_folder_*\n\n**/main_nested.h\n\ngi_keep_file\n!gi_keep_file\nLICENSE\n",
        );
        write(root, "LICENSE", "");
        write(root, "gi_keep_file", "");
        write(root, "gi_file", "");
        write(root, "gi_folder/main.h", "#ifndef");
        write(root, "gi_folder_name/main.h", "#ifndef");
        write(root, "gi_nested_folder/a/b/main_nested.h", "#ifndef");
        write(root, "library.json", r#"{"name": "foo", "version": "1.0.0"}"#);
        PackagePacker::new(root, None).pack(Some(root)).unwrap();
        assert_eq!(
            tar_names(&root.join("foo-1.0.0.tar.gz")),
            set(&["library.json", "LICENSE", ".gitignore", "gi_keep_file"])
        );
    }

    #[test]
    fn test_source_root() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, "root/src/main.cpp", "#include <stdio.h>");
        write(root, "root/library.json", r#"{"name": "bar", "version": "2.0.0"}"#);
        let out = PackagePacker::new(root, None).pack(Some(root)).unwrap();
        assert_eq!(tar_names(&out), set(&["library.json", "src/main.cpp"]));
    }

    #[test]
    fn test_manifest_uri() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        write(root, "root/src/main.cpp", "#include <stdio.h>");
        write(root, "root/library.json", r#"{"name": "foo", "version": "1.0.0"}"#);
        write(root, "root/library/bar/library.json", r#"{"name": "bar", "version": "2.0.0"}"#);
        write(root, "root/library/bar/include/bar.h", "");
        let manifest_path = root.join("remote_library.json");
        fs::write(
            &manifest_path,
            r#"{"name": "bar", "version": "3.0.0", "export": {"include": "root/library/bar"}}"#,
        )
        .unwrap();

        let p = PackagePacker::new(root, Some(format!("file:{}", manifest_path.display())));
        p.pack(Some(root)).unwrap();
        assert_eq!(
            tar_names(&root.join("bar-2.0.0.tar.gz")),
            set(&["library.json", "include/bar.h"])
        );
    }
}
