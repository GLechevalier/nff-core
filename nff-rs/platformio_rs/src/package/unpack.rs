//! Port of `platformio/package/unpack.py` (`FileUnpacker` + the TAR/ZIP
//! archivers).
//!
//! The archive kind is sniffed from magic bytes exactly as upstream. Path
//! traversal is prevented (tar via `Entry::unpack_in`, zip via `enclosed_name`).
//!
//! Documented deviation: bzip2/xz-compressed tarballs are detected but not yet
//! decompressed (upstream leans on Python's `tarfile`); only gzip tar and zip —
//! the formats the registry serves and the tests exercise — are extracted.

use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};

use crate::package::error::{PackageError, Result};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ArchiveKind {
    TarGz,
    TarBz2,
    TarXz,
    Zip,
}

/// `platformio.package.unpack.FileUnpacker`.
pub struct FileUnpacker {
    path: PathBuf,
    kind: ArchiveKind,
}

impl FileUnpacker {
    /// `FileUnpacker(path)` — sniff the archive type from magic bytes.
    pub fn new(path: impl Into<PathBuf>) -> Result<Self> {
        let path = path.into();
        let kind = Self::detect_kind(&path)?;
        Ok(Self { path, kind })
    }

    /// `FileUnpacker.new_archiver` — the magic-byte → archiver map.
    fn detect_kind(path: &Path) -> Result<ArchiveKind> {
        let mut fp = fs::File::open(path).map_err(|e| PackageError::Package { message: e.to_string() })?;
        let mut head = [0u8; 6];
        let n = fp.read(&mut head).map_err(|e| PackageError::Package { message: e.to_string() })?;
        let head = &head[..n];
        if head.starts_with(&[0x1f, 0x8b, 0x08]) {
            Ok(ArchiveKind::TarGz)
        } else if head.starts_with(&[0x42, 0x5a, 0x68]) {
            Ok(ArchiveKind::TarBz2)
        } else if head.starts_with(&[0xfd, 0x37, 0x7a, 0x58, 0x5a, 0x00]) {
            Ok(ArchiveKind::TarXz)
        } else if head.starts_with(&[0x50, 0x4b, 0x03, 0x04]) {
            Ok(ArchiveKind::Zip)
        } else {
            Err(PackageError::Package {
                message: format!("Unknown archive type '{}'", path.display()),
            })
        }
    }

    /// `FileUnpacker.unpack` — extract every member into `dest_dir`. When
    /// `check_unpacked`, verify each non-link member exists afterward.
    pub fn unpack(&self, dest_dir: &Path, check_unpacked: bool) -> Result<bool> {
        fs::create_dir_all(dest_dir).map_err(|e| PackageError::Package { message: e.to_string() })?;
        let members = match self.kind {
            ArchiveKind::TarGz => self.unpack_targz(dest_dir)?,
            ArchiveKind::Zip => self.unpack_zip(dest_dir)?,
            ArchiveKind::TarBz2 | ArchiveKind::TarXz => {
                return Err(PackageError::Package {
                    message: format!(
                        "bzip2/xz archive extraction is not supported yet ('{}')",
                        self.path.display()
                    ),
                });
            }
        };
        if check_unpacked {
            for (name, is_link) in &members {
                if !is_link && !dest_dir.join(name).exists() {
                    return Err(PackageError::Package {
                        message: format!(
                            "Could not extract `{name}` to `{}`. Try to disable antivirus tool \
                             or check this solution -> https://bit.ly/faq-package-manager",
                            dest_dir.display()
                        ),
                    });
                }
            }
        }
        Ok(true)
    }

    fn unpack_targz(&self, dest_dir: &Path) -> Result<Vec<(String, bool)>> {
        let file = fs::File::open(&self.path).map_err(|e| PackageError::Package { message: e.to_string() })?;
        let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(file));
        let mut members = Vec::new();
        for entry in archive.entries().map_err(|e| PackageError::Package { message: e.to_string() })? {
            let mut entry = entry.map_err(|e| PackageError::Package { message: e.to_string() })?;
            let name = entry
                .path()
                .map_err(|e| PackageError::Package { message: e.to_string() })?
                .to_string_lossy()
                .replace('\\', "/");
            let etype = entry.header().entry_type();
            let is_link = etype.is_symlink() || etype.is_hard_link();
            // `unpack_in` refuses paths escaping `dest_dir` (returns Ok(false)).
            entry.unpack_in(dest_dir).map_err(|e| PackageError::Package { message: e.to_string() })?;
            members.push((name.trim_end_matches('/').to_string(), is_link));
        }
        Ok(members)
    }

    fn unpack_zip(&self, dest_dir: &Path) -> Result<Vec<(String, bool)>> {
        let file = fs::File::open(&self.path).map_err(|e| PackageError::Package { message: e.to_string() })?;
        let mut archive =
            zip::ZipArchive::new(file).map_err(|e| PackageError::Package { message: e.to_string() })?;
        let mut members = Vec::new();
        for i in 0..archive.len() {
            let mut zf =
                archive.by_index(i).map_err(|e| PackageError::Package { message: e.to_string() })?;
            // `enclosed_name` returns None for path-traversal entries → skip.
            let Some(rel) = zf.enclosed_name() else { continue };
            let name = rel.to_string_lossy().replace('\\', "/");
            let out = dest_dir.join(&rel);
            if zf.is_dir() {
                fs::create_dir_all(&out).map_err(|e| PackageError::Package { message: e.to_string() })?;
            } else {
                if let Some(parent) = out.parent() {
                    fs::create_dir_all(parent).map_err(|e| PackageError::Package { message: e.to_string() })?;
                }
                let mut outfile =
                    fs::File::create(&out).map_err(|e| PackageError::Package { message: e.to_string() })?;
                std::io::copy(&mut zf, &mut outfile).map_err(|e| PackageError::Package { message: e.to_string() })?;
            }
            members.push((name.trim_end_matches('/').to_string(), false));
        }
        Ok(members)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use flate2::write::GzEncoder;
    use std::io::Write;

    fn write(root: &Path, rel: &str, contents: &str) {
        let p = root.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(p, contents).unwrap();
    }

    #[test]
    fn unpack_targz_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let src = dir.path().join("src");
        write(&src, "library.json", "{}");
        write(&src, "src/main.cpp", "int main(){}");

        let archive = dir.path().join("pkg.tar.gz");
        let enc = GzEncoder::new(fs::File::create(&archive).unwrap(), flate2::Compression::default());
        let mut tar = tar::Builder::new(enc);
        tar.append_path_with_name(src.join("library.json"), "library.json").unwrap();
        tar.append_path_with_name(src.join("src/main.cpp"), "src/main.cpp").unwrap();
        tar.into_inner().unwrap().finish().unwrap();

        let dest = dir.path().join("out");
        assert!(FileUnpacker::new(&archive).unwrap().unpack(&dest, true).unwrap());
        assert!(dest.join("library.json").is_file());
        assert!(dest.join("src/main.cpp").is_file());
    }

    #[test]
    fn unpack_zip_roundtrip() {
        let dir = tempfile::tempdir().unwrap();
        let archive = dir.path().join("pkg.zip");
        {
            let mut zw = zip::ZipWriter::new(fs::File::create(&archive).unwrap());
            let opts: zip::write::FileOptions<'_, ()> = zip::write::FileOptions::default();
            zw.start_file("library.json", opts).unwrap();
            zw.write_all(b"{}").unwrap();
            zw.start_file("src/main.cpp", opts).unwrap();
            zw.write_all(b"int main(){}").unwrap();
            zw.finish().unwrap();
        }
        let dest = dir.path().join("out");
        assert!(FileUnpacker::new(&archive).unwrap().unpack(&dest, true).unwrap());
        assert!(dest.join("library.json").is_file());
        assert!(dest.join("src/main.cpp").is_file());
    }

    #[test]
    fn detects_unknown_archive() {
        let dir = tempfile::tempdir().unwrap();
        let f = dir.path().join("plain.txt");
        fs::write(&f, "not an archive").unwrap();
        assert!(FileUnpacker::new(&f).is_err());
    }
}
