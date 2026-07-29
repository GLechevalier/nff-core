//! Rust port of the parser half of `tests/package/test_manifest.py`.
//!
//! The schema-driven tests (`test_*_schema`, `test_examples_from_dir`,
//! `test_broken_schemas`) land with the schema module (M2b-2).

use serde_json::{json, Value};

use crate::package::error::PackageError;
use crate::package::manifest::parser::{ManifestFileType, ManifestParserFactory};
use crate::package::manifest::schema::ManifestSchema;

/// Parse via the factory for a given manifest type.
fn parse(contents: &str, manifest_type: &str) -> Value {
    ManifestParserFactory::new(contents, manifest_type, None, None).unwrap().as_dict()
}

/// Sort the `dependencies` array by `name` (as the upstream tests do before
/// comparing), so order differences don't matter.
fn sort_deps(mut v: Value) -> Value {
    if let Some(deps) = v.get_mut("dependencies").and_then(Value::as_array_mut) {
        deps.sort_by(|a, b| {
            let na = a.get("name").and_then(Value::as_str).unwrap_or("");
            let nb = b.get("name").and_then(Value::as_str).unwrap_or("");
            na.cmp(nb)
        });
    }
    v
}

#[test]
fn test_library_json_parser() {
    let contents = r#"
{
    "name": "TestPackage",
    "keywords": "kw1, KW2, kw3, KW2, kw 4, kw_5, kw-6",
    "headers": "include1.h, Include2.hpp",
    "platforms": ["atmelavr", "espressif"],
    "repository": {
        "type": "git",
        "url": "http://github.com/username/repo/"
    },
    "url": "http://old.url.format",
    "exclude": [".gitignore", "tests"],
    "include": "mylib",
    "build": {
        "flags": ["-DHELLO"]
    },
    "examples": ["examples/*/*.pde"],
    "dependencies": {
        "deps1": "1.2.0",
        "deps2": "https://github.com/username/package.git",
        "owner/deps3": "^2.1.3"
    },
    "customField": "Custom Value"
}
"#;
    let raw = sort_deps(parse(contents, ManifestFileType::LIBRARY_JSON));
    assert_eq!(
        raw,
        json!({
            "name": "TestPackage",
            "platforms": ["atmelavr", "espressif8266"],
            "repository": {"type": "git", "url": "https://github.com/username/repo.git"},
            "export": {"exclude": [".gitignore", "tests"], "include": ["mylib"]},
            "keywords": ["kw1", "kw2", "kw3", "kw 4", "kw_5", "kw-6"],
            "headers": ["include1.h", "Include2.hpp"],
            "homepage": "http://old.url.format",
            "build": {"flags": ["-DHELLO"]},
            "dependencies": [
                {"name": "deps1", "version": "1.2.0"},
                {"name": "deps2", "version": "https://github.com/username/package.git"},
                {"owner": "owner", "name": "deps3", "version": "^2.1.3"},
            ],
            "customField": "Custom Value",
        })
    );

    let contents = r#"
{
    "keywords": ["sound", "audio", "music", "SD", "card", "playback"],
    "headers": ["include 1.h", "include Space.hpp"],
    "frameworks": "arduino",
    "export": {
        "exclude": "audio_samples"
    },
    "dependencies": [
        {"name": "deps1", "version": "1.0.0"},
        {"name": "deps2", "version": "1.0.0", "platforms": "*", "frameworks": "arduino, espidf", "owner": "owner"},
        {"name": "deps3", "version": "1.0.0", "platforms": ["ststm32", "sifive"]}
    ]
}
"#;
    let raw = sort_deps(parse(contents, ManifestFileType::LIBRARY_JSON));
    assert_eq!(
        raw,
        json!({
            "keywords": ["sound", "audio", "music", "sd", "card", "playback"],
            "headers": ["include 1.h", "include Space.hpp"],
            "frameworks": ["arduino"],
            "export": {"exclude": ["audio_samples"]},
            "dependencies": [
                {"name": "deps1", "version": "1.0.0"},
                {"owner": "owner", "name": "deps2", "version": "1.0.0", "platforms": ["*"], "frameworks": ["arduino", "espidf"]},
                {"name": "deps3", "version": "1.0.0", "platforms": ["ststm32", "sifive"]},
            ],
        })
    );

    let raw = sort_deps(parse(
        r#"{"dependencies": ["dep1", "dep2", "owner/dep3@1.2.3"]}"#,
        ManifestFileType::LIBRARY_JSON,
    ));
    assert_eq!(
        raw,
        json!({"dependencies": [{"name": "dep1"}, {"name": "dep2"}, {"name": "owner/dep3@1.2.3"}]})
    );

    // broken manifest content (Python passes a non-str; we feed invalid JSON)
    assert!(ManifestParserFactory::new("not json", ManifestFileType::LIBRARY_JSON, None, None).is_err());
}

#[test]
fn test_module_json_parser() {
    let contents = r#"
{
  "author": "Name Surname <name@surname.com>",
  "description": "This is Yotta library",
  "homepage": "https://yottabuild.org",
  "keywords": ["mbed", "Yotta"],
  "licenses": [{"type": "Apache-2.0", "url": "https://spdx.org/licenses/Apache-2.0"}],
  "name": "YottaLibrary",
  "repository": {"type": "git", "url": "git@github.com:username/repo.git"},
  "version": "1.2.3",
  "dependencies": {"usefulmodule": "^1.2.3", "simplelog": "ARMmbed/simplelog#~0.0.1"},
  "customField": "Custom Value"
}
"#;
    let raw = sort_deps(parse(contents, ManifestFileType::MODULE_JSON));
    assert_eq!(
        raw,
        json!({
            "name": "YottaLibrary",
            "description": "This is Yotta library",
            "homepage": "https://yottabuild.org",
            "keywords": ["mbed", "yotta"],
            "license": "Apache-2.0",
            "platforms": ["*"],
            "frameworks": ["mbed"],
            "export": {"exclude": ["tests", "test", "*.doxyfile", "*.pdf"]},
            "authors": [{"email": "name@surname.com", "name": "Name Surname"}],
            "version": "1.2.3",
            "repository": {"type": "git", "url": "git@github.com:username/repo.git"},
            "dependencies": [
                {"name": "simplelog", "version": "ARMmbed/simplelog#~0.0.1", "frameworks": ["mbed"]},
                {"name": "usefulmodule", "version": "^1.2.3", "frameworks": ["mbed"]},
            ],
            "customField": "Custom Value",
        })
    );
}

#[test]
fn test_library_properties_parser() {
    let contents = "
name=TestPackage
version=1.2.3
author=SomeAuthor <info AT author.com>, Maintainer Author (nickname) <www.example.com>
maintainer=Maintainer Author (nickname) <www.example.com>
sentence=This is Arduino library
category=Signal Input/Output
customField=Custom Value
depends=First Library (=2.0.0), Second Library (>=1.2.0), Third
ignore_empty_field=
includes=Arduino.h, Arduino Space.hpp
";
    let raw = sort_deps(parse(contents, ManifestFileType::LIBRARY_PROPERTIES));
    assert_eq!(
        raw,
        json!({
            "name": "TestPackage",
            "version": "1.2.3",
            "description": "This is Arduino library",
            "sentence": "This is Arduino library",
            "frameworks": ["arduino"],
            "authors": [
                {"name": "SomeAuthor", "email": "info@author.com"},
                {"name": "Maintainer Author", "maintainer": true},
            ],
            "category": "Signal Input/Output",
            "keywords": ["signal", "input", "output"],
            "headers": ["Arduino.h", "Arduino Space.hpp"],
            "includes": "Arduino.h, Arduino Space.hpp",
            "customField": "Custom Value",
            "depends": "First Library (=2.0.0), Second Library (>=1.2.0), Third",
            "dependencies": [
                {"name": "First Library", "version": "=2.0.0", "frameworks": ["arduino"]},
                {"name": "Second Library", "version": ">=1.2.0", "frameworks": ["arduino"]},
                {"name": "Third", "frameworks": ["arduino"]},
            ],
        })
    );

    // Platforms ALL
    let data = parse(&format!("architectures=*\n{contents}"), ManifestFileType::LIBRARY_PROPERTIES);
    assert_eq!(data["platforms"], json!(["*"]));

    // Platforms specific
    let data =
        parse(&format!("architectures=avr, esp32\n{contents}"), ManifestFileType::LIBRARY_PROPERTIES);
    assert_eq!(data["platforms"], json!(["atmelavr", "espressif32"]));

    // Remote URL
    let data = ManifestParserFactory::new(
        contents,
        ManifestFileType::LIBRARY_PROPERTIES,
        Some("https://raw.githubusercontent.com/username/reponame/master/libraries/TestPackage/library.properties"),
        None,
    )
    .unwrap()
    .as_dict();
    assert_eq!(data["export"], json!({"include": ["libraries/TestPackage"]}));
    assert_eq!(data["repository"], json!({"url": "https://github.com/username/reponame.git", "type": "git"}));

    // Home page
    let data = parse(
        &format!("url=https://github.com/username/reponame.git\n{contents}"),
        ManifestFileType::LIBRARY_PROPERTIES,
    );
    assert_eq!(data["repository"], json!({"type": "git", "url": "https://github.com/username/reponame.git"}));

    // Author + Maintainer
    let data = parse(
        "
author=Rocket Scream Electronics <broken-email.com>
maintainer=Rocket Scream Electronics
",
        ManifestFileType::LIBRARY_PROPERTIES,
    );
    assert_eq!(data["authors"], json!([{"name": "Rocket Scream Electronics", "maintainer": true}]));
    assert!(data.get("keywords").is_none());
}

#[test]
fn test_parser_from_dir() {
    let pkg_dir = tempfile::tempdir().unwrap();
    std::fs::write(pkg_dir.path().join("package.json"), r#"{"name": "package.json"}"#).unwrap();
    std::fs::write(pkg_dir.path().join("library.json"), r#"{"name": "library.json"}"#).unwrap();
    std::fs::write(pkg_dir.path().join("library.properties"), "name=library.properties").unwrap();

    let data = ManifestParserFactory::new_from_dir(pkg_dir.path(), None).unwrap().as_dict();
    assert_eq!(data["name"], json!("library.json"));

    let data = ManifestParserFactory::new_from_dir(
        pkg_dir.path(),
        Some("http://localhost/library.properties"),
    )
    .unwrap()
    .as_dict();
    assert_eq!(data["name"], json!("library.properties"));
}

#[test]
fn test_parser_from_archive() {
    use flate2::write::GzEncoder;
    use flate2::Compression;

    let pkg_dir = tempfile::tempdir().unwrap();
    std::fs::write(pkg_dir.path().join("package.json"), r#"{"name": "package.json"}"#).unwrap();
    std::fs::write(pkg_dir.path().join("library.json"), r#"{"name": "library.json"}"#).unwrap();
    std::fs::write(pkg_dir.path().join("library.properties"), "name=library.properties").unwrap();

    let archive_path = pkg_dir.path().join("package.tar.gz");
    let file = std::fs::File::create(&archive_path).unwrap();
    let enc = GzEncoder::new(file, Compression::default());
    let mut builder = tar::Builder::new(enc);
    for item in ["package.json", "library.json", "library.properties"] {
        builder.append_path_with_name(pkg_dir.path().join(item), item).unwrap();
    }
    builder.into_inner().unwrap().finish().unwrap();

    let data = ManifestParserFactory::new_from_archive(&archive_path).unwrap().as_dict();
    assert_eq!(data["name"], json!("library.json"));
}

// --- schema-driven tests ---------------------------------------------------

fn load(raw: &Value) -> Value {
    ManifestSchema::new().load_manifest(raw).unwrap()
}

/// Return `(messages, valid_data)` from a `ManifestValidationError`, plus the
/// error's `Display` string for `match=`-style substring checks.
fn validation_error(raw: Value) -> (Value, Value, String) {
    match ManifestSchema::new().load_manifest(&raw) {
        Err(e @ PackageError::ManifestValidation { .. }) => {
            let display = e.to_string();
            let PackageError::ManifestValidation { messages, valid_data } = e else { unreachable!() };
            (messages, valid_data, display)
        }
        other => panic!("expected ManifestValidationError, got {other:?}"),
    }
}

#[test]
fn test_library_json_schema() {
    let contents = r#"
{
  "name": "ArduinoJson",
  "keywords": "JSON, rest, http, web",
  "description": "An elegant and efficient JSON library for embedded systems",
  "homepage": "https://arduinojson.org",
  "repository": {"type": "git", "url": "https://github.com/bblanchon/ArduinoJson.git"},
  "version": "6.12.0",
  "authors": {"name": "Benoit Blanchon", "url": "https://blog.benoitblanchon.fr"},
  "downloadUrl": "https://example.com/package.tar.gz",
  "exclude": ["fuzzing", "scripts", "test", "third-party"],
  "frameworks": "arduino",
  "platforms": "*",
  "license": "MIT",
  "scripts": {"postinstall": "script.py"},
  "examples": [
    {"name": "JsonConfigFile", "base": "examples/JsonConfigFile", "files": ["JsonConfigFile.ino"]},
    {"name": "JsonHttpClient", "base": "examples/JsonHttpClient", "files": ["JsonHttpClient.ino"]}
  ],
  "dependencies": [
    {"name": "deps1", "version": "1.0.0"},
    {"name": "@owner/deps2", "version": "1.0.0", "frameworks": "arduino"},
    {"name": "deps3", "version": "1.0.0", "platforms": ["ststm32", "sifive"]}
  ]
}
"#;
    let raw = sort_deps(parse(contents, ManifestFileType::LIBRARY_JSON));
    let data = load(&raw);
    assert_eq!(data["repository"]["url"], json!("https://github.com/bblanchon/ArduinoJson.git"));
    assert_eq!(data["examples"][1]["base"], json!("examples/JsonHttpClient"));
    assert_eq!(data["examples"][1]["files"], json!(["JsonHttpClient.ino"]));
    assert_eq!(
        data,
        json!({
            "name": "ArduinoJson",
            "keywords": ["json", "rest", "http", "web"],
            "description": "An elegant and efficient JSON library for embedded systems",
            "homepage": "https://arduinojson.org",
            "repository": {"url": "https://github.com/bblanchon/ArduinoJson.git", "type": "git"},
            "version": "6.12.0",
            "authors": [{"name": "Benoit Blanchon", "url": "https://blog.benoitblanchon.fr"}],
            "downloadUrl": "https://example.com/package.tar.gz",
            "export": {"exclude": ["fuzzing", "scripts", "test", "third-party"]},
            "frameworks": ["arduino"],
            "platforms": ["*"],
            "license": "MIT",
            "scripts": {"postinstall": "script.py"},
            "examples": [
                {"name": "JsonConfigFile", "base": "examples/JsonConfigFile", "files": ["JsonConfigFile.ino"]},
                {"name": "JsonHttpClient", "base": "examples/JsonHttpClient", "files": ["JsonHttpClient.ino"]},
            ],
            "dependencies": [
                {"name": "@owner/deps2", "version": "1.0.0", "frameworks": ["arduino"]},
                {"name": "deps1", "version": "1.0.0"},
                {"name": "deps3", "version": "1.0.0", "platforms": ["ststm32", "sifive"]},
            ],
        })
    );

    // legacy single-dependency dict
    let raw = parse(
        r#"{"name": "DallasTemperature", "version": "3.8.0", "dependencies": {"name": "OneWire", "authors": "Paul Stoffregen", "frameworks": "arduino"}}"#,
        ManifestFileType::LIBRARY_JSON,
    );
    assert_eq!(
        load(&raw),
        json!({
            "name": "DallasTemperature",
            "version": "3.8.0",
            "dependencies": [{"name": "OneWire", "authors": ["Paul Stoffregen"], "frameworks": ["arduino"]}],
        })
    );

    // multiple licenses (SPDX expression)
    let raw = parse(
        r#"{"name": "MultiLicense", "version": "1.0.0", "license": "MIT AND (LGPL-2.1-or-later OR BSD-3-Clause)"}"#,
        ManifestFileType::LIBRARY_JSON,
    );
    assert_eq!(
        load(&raw),
        json!({"name": "MultiLicense", "version": "1.0.0", "license": "MIT AND (LGPL-2.1-or-later OR BSD-3-Clause)"})
    );
}

#[test]
fn test_library_properties_schema() {
    let contents = "
name=U8glib
version=1.19.1
author=oliver <olikraus@gmail.com>
maintainer=oliver <olikraus@gmail.com>
sentence=A library for monochrome TFTs and OLEDs
paragraph=Supported display controller: SSD1306, SSD1309, SSD1322, SSD1325
category=Display
url=https://github.com/olikraus/u8glib
architectures=avr,sam,samd
depends=First Library (=2.0.0), Second Library (>=1.2.0), Third
";
    let raw = sort_deps(parse(contents, ManifestFileType::LIBRARY_PROPERTIES));
    assert_eq!(
        load(&raw),
        json!({
            "description": "A library for monochrome TFTs and OLEDs. Supported display controller: SSD1306, SSD1309, SSD1322, SSD1325",
            "repository": {"url": "https://github.com/olikraus/u8glib.git", "type": "git"},
            "frameworks": ["arduino"],
            "platforms": ["atmelavr", "atmelsam"],
            "version": "1.19.1",
            "authors": [{"maintainer": true, "email": "olikraus@gmail.com", "name": "oliver"}],
            "keywords": ["display"],
            "name": "U8glib",
            "dependencies": [
                {"name": "First Library", "version": "=2.0.0", "frameworks": ["arduino"]},
                {"name": "Second Library", "version": ">=1.2.0", "frameworks": ["arduino"]},
                {"name": "Third", "frameworks": ["arduino"]},
            ],
        })
    );

    // Broken fields: the over-long author is dropped, keeping the maintainer.
    let contents = "
name=Mozzi
version=1.0.3
author=Lorem Ipsum is simply dummy text of the printing and typesetting industry Lorem Ipsum has been the industry's standard dummy text ever since the 1500s  when an unknown printer took a galley of type and scrambled it to make a type specimen book. It has survived not only five centuries  but also the leap into electronic typesetting  remaining essentially unchanged. It was popularised in the 1960s with the release of Letraset sheets containing Lorem Ipsum passages  and more recently with desktop publishing software like Aldus PageMaker including versions of Lorem Ipsum.
maintainer=Tim Barrass <faveflave@gmail.com>
sentence=Sound synthesis library for Arduino
paragraph=With Mozzi, you can construct sounds using familiar synthesis units like oscillators, delays, filters and envelopes.
category=Signal Input/Output
url=https://sensorium.github.io/Mozzi/
architectures=*
dot_a_linkage=false
includes=MozziGuts.h
";
    let raw = ManifestParserFactory::new(
        contents,
        ManifestFileType::LIBRARY_PROPERTIES,
        Some("https://raw.githubusercontent.com/sensorium/Mozzi/master/library.properties"),
        None,
    )
    .unwrap()
    .as_dict();
    let (messages, valid_data, _display) = validation_error(raw);
    assert!(messages.get("authors").is_some());
    assert_eq!(
        valid_data,
        json!({
            "name": "Mozzi",
            "version": "1.0.3",
            "description": "Sound synthesis library for Arduino. With Mozzi, you can construct sounds using familiar synthesis units like oscillators, delays, filters and envelopes.",
            "repository": {"url": "https://github.com/sensorium/Mozzi.git", "type": "git"},
            "platforms": ["*"],
            "frameworks": ["arduino"],
            "headers": ["MozziGuts.h"],
            "authors": [{"maintainer": true, "email": "faveflave@gmail.com", "name": "Tim Barrass"}],
            "keywords": ["signal", "input", "output"],
            "homepage": "https://sensorium.github.io/Mozzi/",
        })
    );
}

#[test]
fn test_platform_json_schema() {
    let contents = r#"
{
  "name": "atmelavr",
  "title": "Atmel AVR",
  "description": "Atmel AVR 8- and 32-bit MCUs.",
  "keywords": "arduino, atmel, avr, MCU",
  "homepage": "http://www.atmel.com/products/microcontrollers/avr/default.aspx",
  "license": "Apache-2.0",
  "engines": {"platformio": "<5"},
  "repository": {"type": "git", "url": "https://github.com/platformio/platform-atmelavr.git"},
  "version": "1.15.0",
  "frameworks": {
    "arduino": {"package": "framework-arduinoavr", "script": "builder/frameworks/arduino.py"},
    "simba": {"package": "framework-simba", "script": "builder/frameworks/simba.py"}
  },
  "packages": {
    "toolchain-atmelavr": {"type": "toolchain", "owner": "platformio", "version": "~1.50400.0"},
    "framework-arduinoavr": {"type": "framework", "optional": true, "version": "~4.2.0"},
    "tool-avrdude": {"type": "uploader", "optional": true, "version": "~1.60300.0"}
  }
}
"#;
    let raw = sort_deps(parse(contents, ManifestFileType::PLATFORM_JSON));
    let data = load(&raw);
    assert_eq!(
        data,
        json!({
            "name": "atmelavr",
            "title": "Atmel AVR",
            "description": "Atmel AVR 8- and 32-bit MCUs.",
            "keywords": ["arduino", "atmel", "avr", "mcu"],
            "homepage": "http://www.atmel.com/products/microcontrollers/avr/default.aspx",
            "license": "Apache-2.0",
            "repository": {"url": "https://github.com/platformio/platform-atmelavr.git", "type": "git"},
            "frameworks": ["arduino", "simba"],
            "version": "1.15.0",
            "dependencies": [
                {"name": "framework-arduinoavr", "version": "~4.2.0"},
                {"name": "tool-avrdude", "version": "~1.60300.0"},
                {"name": "toolchain-atmelavr", "owner": "platformio", "version": "~1.50400.0"},
            ],
        })
    );
}

#[test]
fn test_package_json_schema() {
    let contents = r#"
{
    "name": "tool-scons",
    "description": "SCons software construction tool",
    "keywords": "SCons, build",
    "homepage": "http://www.scons.org",
    "system": ["linux_armv6l", "linux_armv7l", "linux_armv8l", "LINUX_ARMV7L"],
    "version": "3.30101.0"
}
"#;
    let raw = parse(contents, ManifestFileType::PACKAGE_JSON);
    assert_eq!(
        load(&raw),
        json!({
            "name": "tool-scons",
            "description": "SCons software construction tool",
            "keywords": ["scons", "build"],
            "homepage": "http://www.scons.org",
            "system": ["linux_armv6l", "linux_armv7l", "linux_armv8l"],
            "version": "3.30101.0",
        })
    );

    // parser-level system handling
    assert!(parse(r#"{"system": "*"}"#, ManifestFileType::PACKAGE_JSON).get("system").is_none());
    assert!(parse(r#"{"system": "all"}"#, ManifestFileType::PACKAGE_JSON).get("system").is_none());
    assert_eq!(
        parse(r#"{"system": "darwin_x86_64"}"#, ManifestFileType::PACKAGE_JSON)["system"],
        json!(["darwin_x86_64"])
    );

    // npm-style shortcut repository syntax
    let raw = parse(
        r#"{"name": "tool-github", "version": "1.2.0", "repository": "github:user/repo"}"#,
        ManifestFileType::PACKAGE_JSON,
    );
    assert_eq!(load(&raw)["repository"]["url"], json!("https://github.com/user/repo.git"));
}

#[test]
fn test_broken_schemas() {
    // invalid semantic version
    let (_messages, valid_data, display) =
        validation_error(json!({"name": "MyPackage", "version": "broken_version"}));
    assert!(display.contains("Invalid semantic versioning format"));
    assert_eq!(valid_data, json!({"name": "MyPackage"}));

    // invalid StrictList item (dropped, valid subset kept)
    let (messages, valid_data, display) =
        validation_error(json!({"name": "MyPackage", "version": "1.0.0", "keywords": ["kw1", "*^[]"]}));
    assert!(display.contains("Invalid manifest fields") && display.contains("keywords"));
    let keys: Vec<&String> = messages.as_object().unwrap().keys().collect();
    assert_eq!(keys, ["keywords"]);
    assert_eq!(valid_data["keywords"], json!(["kw1"]));

    // version with leading zeros
    let (_m, _vd, display) = validation_error(json!({"name": "MyPackage", "version": "01.02.00"}));
    assert!(display.contains("Invalid semantic versioning format"));

    // broken value for Nested (author is a string, not a dict)
    let (_m, _vd, display) = validation_error(json!({
        "name": "MyPackage",
        "description": "MyDescription",
        "keywords": ["a", "b"],
        "authors": ["should be dict here"],
        "version": "1.2.3",
    }));
    assert!(display.contains("authors") && display.contains("Invalid input type"));

    // invalid package name
    let (_m, _vd, display) = validation_error(json!({"name": "C/C++ :library", "version": "1.2.3"}));
    assert!(display.contains("are not allowed"));
}

#[test]
fn test_examples_from_dir() {
    let package = tempfile::tempdir().unwrap();
    let root = package.path();
    let w = |rel: &str, contents: &str| {
        let p = root.join(rel);
        std::fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::fs::write(p, contents).unwrap();
    };
    w("library.json", r#"{"name": "pkg", "version": "1.0.0", "examples": ["examples/*/*.pde"]}"#);

    // PlatformIO project #1
    w("examples/PlatformIO/hello/.vimrc", "");
    w("examples/PlatformIO/hello/platformio.ini", "");
    w("examples/PlatformIO/hello/include/main.h", "");
    w("examples/PlatformIO/hello/src/main.cpp", "");
    // wiring examples
    w("examples/1. General/SomeSketchIno/SomeSketchIno.ino", "");
    w("examples/1. General/SomeSketchPde/SomeSketchPde.pde", "");
    // custom examples
    w("examples/demo/demo.cpp", "");
    w("examples/demo/demo.h", "");
    w("examples/demo/util.h", "");
    // PlatformIO project #2
    w("examples/world/platformio.ini", "");
    w("examples/world/README", "");
    w("examples/world/extra.py", "");
    w("examples/world/include/world.h", "");
    w("examples/world/src/world.c", "");
    // example files in root
    w("examples/root.c", "");
    w("examples/root.h", "");
    // invalid example (empty dir)
    std::fs::create_dir_all(root.join("examples/invalid-example")).unwrap();

    let raw = ManifestParserFactory::new_from_dir(root, None).unwrap().as_dict();
    assert_eq!(raw["examples"].as_array().unwrap().len(), 6);

    let data = load(&raw);
    let mut got = data["examples"].clone();
    let mut want = json!([
        {"name": "PlatformIO/hello", "base": "examples/PlatformIO/hello", "files": ["platformio.ini", "include/main.h", "src/main.cpp"]},
        {"name": "1_General/SomeSketchIno", "base": "examples/1. General/SomeSketchIno", "files": ["SomeSketchIno.ino"]},
        {"name": "1_General/SomeSketchPde", "base": "examples/1. General/SomeSketchPde", "files": ["SomeSketchPde.pde"]},
        {"name": "demo", "base": "examples/demo", "files": ["demo.h", "util.h", "demo.cpp"]},
        {"name": "world", "base": "examples/world", "files": ["platformio.ini", "include/world.h", "src/world.c", "README", "extra.py"]},
        {"name": "Examples", "base": "examples", "files": ["root.c", "root.h"]},
    ]);
    sort_examples(&mut got);
    sort_examples(&mut want);
    assert_eq!(got, want);
    assert_eq!(data["name"], json!("pkg"));
    assert_eq!(data["version"], json!("1.0.0"));
}

/// Mirror of the test's `_sort_examples`: unix-normalize `base`, sort+normalize
/// `files`, and sort items by `name`.
fn sort_examples(examples: &mut Value) {
    let arr = examples.as_array_mut().unwrap();
    for item in arr.iter_mut() {
        if let Some(base) = item.get("base").and_then(Value::as_str) {
            let unix = base.replace('\\', "/");
            item["base"] = json!(unix);
        }
        if let Some(files) = item.get_mut("files").and_then(Value::as_array_mut) {
            let mut fs: Vec<String> =
                files.iter().filter_map(|f| f.as_str().map(|s| s.replace('\\', "/"))).collect();
            fs.sort();
            *files = fs.into_iter().map(Value::String).collect();
        }
    }
    arr.sort_by(|a, b| {
        a.get("name").and_then(Value::as_str).unwrap_or("").cmp(b.get("name").and_then(Value::as_str).unwrap_or(""))
    });
}
