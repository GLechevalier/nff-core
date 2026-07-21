//! Port of `platformio/registry/`: the registry API client and the file-mirror
//! redirect iterator. Network-only — see [`crate::http`] for the deviations.

pub mod client;
pub mod mirror;

pub use client::RegistryClient;
pub use mirror::RegistryFileMirrorIterator;
