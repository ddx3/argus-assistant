// swift-tools-version:5.7
//
// argus-syscap is a small program that gives Argus the system audio on macOS.
// Build: `swift build -c release` in this folder; the result is
// `.build/release/argus-syscap`.
// Details and the contract (what goes to stdout, what to stderr) are in
// `Sources/ArgusSyscap/main.swift` and in `docs/macos.md`.

import PackageDescription

let package = Package(
    name: "argus-syscap",
    platforms: [.macOS(.v13)],
    products: [
        .executable(name: "argus-syscap", targets: ["ArgusSyscap"]),
    ],
    targets: [
        .executableTarget(
            name: "ArgusSyscap",
            path: "Sources/ArgusSyscap"
        ),
    ]
)
