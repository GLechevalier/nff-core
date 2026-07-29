//! Download helpers ported from `platformio/package/download.py` (the checksum
//! verification) and `platformio/package/manager/_download.py` (the
//! content-addressed cache path).
//!
//! The offline-verifiable slice lives here: `calculate_file_hashsum`,
//! `verify_checksum` (the algorithm-by-length logic of `FileDownloader.verify`),
//! and `compute_download_path` (the SHA1 cache filename). The networked
//! `FileDownloader.start` / `HTTPSession` land with the registry/http layer.

use std::io::Read;
use std::path::{Path, PathBuf};

use md5::Md5;
use sha1::{Digest, Sha1};
use sha2::Sha256;

use crate::package::error::{PackageError, Result};

/// Lowercase hex of a byte slice.
fn to_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{b:02x}"));
    }
    s
}

/// `platformio.fs.calculate_file_hashsum` — streamed hex digest. Supports the
/// three algorithms `FileDownloader.verify` selects (`md5`/`sha1`/`sha256`).
pub fn calculate_file_hashsum(algorithm: &str, path: &Path) -> Result<String> {
    let mut file =
        std::fs::File::open(path).map_err(|e| PackageError::Package { message: e.to_string() })?;
    let mut buf = [0u8; 8192];

    macro_rules! stream {
        ($hasher:expr) => {{
            let mut hasher = $hasher;
            loop {
                let n = file.read(&mut buf).map_err(|e| PackageError::Package { message: e.to_string() })?;
                if n == 0 {
                    break;
                }
                hasher.update(&buf[..n]);
            }
            to_hex(&hasher.finalize())
        }};
    }

    Ok(match algorithm {
        "md5" => stream!(Md5::new()),
        "sha1" => stream!(Sha1::new()),
        "sha256" => stream!(Sha256::new()),
        other => {
            return Err(PackageError::Package {
                message: format!("Unsupported hash algorithm '{other}'"),
            })
        }
    })
}

/// The checksum half of `FileDownloader.verify`: pick the algorithm by the
/// checksum's hex length, then compare case-insensitively.
pub fn verify_checksum(path: &Path, checksum: &str) -> Result<()> {
    let algo = match checksum.len() {
        32 => "md5",
        40 => "sha1",
        64 => "sha256",
        _ => {
            return Err(PackageError::Package {
                message: format!("Could not determine checksum algorithm by {checksum}"),
            })
        }
    };
    let actual = calculate_file_hashsum(algo, path)?;
    if actual.eq_ignore_ascii_case(checksum) {
        Ok(())
    } else {
        Err(PackageError::Package {
            message: format!(
                "The checksum '{actual}' of the downloaded file does not match to the remote '{checksum}'"
            ),
        })
    }
}

/// `PackageManagerDownloadMixin.compute_download_path` — a SHA1 over the joined
/// args (called with `(url, checksum or "")`), placed in the downloads dir.
#[must_use]
pub fn compute_download_path(download_dir: &Path, url: &str, checksum: Option<&str>) -> PathBuf {
    let mut hasher = Sha1::new();
    hasher.update(url.as_bytes());
    hasher.update(checksum.unwrap_or("").as_bytes());
    download_dir.join(to_hex(&hasher.finalize()))
}

/// `FileDownloader` filename derivation from a URL (the `content-disposition`
/// path is only available with a live response, so this is the URL fallback).
#[must_use]
pub fn filename_from_url(url: &str) -> String {
    url.split('/').rfind(|p| !p.is_empty()).unwrap_or("").to_string()
}

/// `FileDownloader.start` — stream `url` to `dest` over HTTP. Only status
/// 200/203 are accepted (the upstream contract). Network-only; not unit-tested.
pub fn http_download(url: &str, dest: &Path) -> Result<()> {
    let client = reqwest::blocking::Client::builder()
        .user_agent(concat!("platformio_rs/", env!("CARGO_PKG_VERSION")))
        .build()
        .map_err(|e| PackageError::Package { message: e.to_string() })?;
    let mut resp = client.get(url).send().map_err(|e| PackageError::Package { message: e.to_string() })?;
    let status = resp.status().as_u16();
    if status != 200 && status != 203 {
        return Err(PackageError::Package {
            message: format!("Got the unrecognized status code '{status}' when downloaded {url}"),
        });
    }
    let mut file = std::fs::File::create(dest).map_err(|e| PackageError::Package { message: e.to_string() })?;
    std::io::copy(&mut resp, &mut file).map_err(|e| PackageError::Package { message: e.to_string() })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hashsum_matches_known_vectors() {
        let dir = tempfile::tempdir().unwrap();
        let f = dir.path().join("abc.txt");
        std::fs::write(&f, "abc").unwrap();
        // Known digests of "abc".
        assert_eq!(calculate_file_hashsum("md5", &f).unwrap(), "900150983cd24fb0d6963f7d28e17f72");
        assert_eq!(calculate_file_hashsum("sha1", &f).unwrap(), "a9993e364706816aba3e25717850c26c9cd0d89d");
        assert_eq!(
            calculate_file_hashsum("sha256", &f).unwrap(),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        );
    }

    #[test]
    fn verify_checksum_by_length() {
        let dir = tempfile::tempdir().unwrap();
        let f = dir.path().join("abc.txt");
        std::fs::write(&f, "abc").unwrap();
        // sha256 (len 64)
        assert!(verify_checksum(&f, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad").is_ok());
        // sha1 (len 40), case-insensitive
        assert!(verify_checksum(&f, "A9993E364706816ABA3E25717850C26C9CD0D89D").is_ok());
        // wrong checksum
        assert!(verify_checksum(&f, &"0".repeat(64)).is_err());
        // unknown length
        assert!(verify_checksum(&f, "abcd").is_err());
    }

    #[test]
    fn download_path_is_content_addressed() {
        let dir = tempfile::tempdir().unwrap();
        let a = compute_download_path(dir.path(), "https://x/pkg.zip", Some("deadbeef"));
        let b = compute_download_path(dir.path(), "https://x/pkg.zip", Some("deadbeef"));
        let c = compute_download_path(dir.path(), "https://x/other.zip", Some("deadbeef"));
        assert_eq!(a, b);
        assert_ne!(a, c);
        assert_eq!(a.file_name().unwrap().to_string_lossy().len(), 40); // sha1 hex
        assert!(a.starts_with(dir.path()));
    }

    #[test]
    fn filename_from_url_takes_last_segment() {
        assert_eq!(filename_from_url("https://x/a/b/pkg.tar.gz"), "pkg.tar.gz");
        assert_eq!(filename_from_url("https://x/a/b/"), "b");
    }
}
