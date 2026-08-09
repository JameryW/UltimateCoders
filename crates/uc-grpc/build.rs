//! Build script — compiles protobuf definitions using tonic-build.

fn main() -> Result<(), Box<dyn std::error::Error>> {
    // Docker and CI install protoc system-wide. For local development, use a
    // platform-matched vendored binary so `pip install -e ".[test]"` does not
    // fail on a clean Windows machine.
    if std::env::var_os("PROTOC").is_none() {
        std::env::set_var("PROTOC", protoc_bin_vendored::protoc_bin_path()?);
    }
    tonic_build::configure()
        .build_server(true)
        .build_client(true)
        .compile_protos(&["proto/engine.proto"], &["proto/"])?;
    Ok(())
}
