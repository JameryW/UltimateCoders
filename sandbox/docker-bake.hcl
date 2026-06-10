// Docker Bake configuration for building sandbox images
// Usage: docker buildx bake -f sandbox/docker-bake.hcl

group "default" {
    targets = ["sandbox"]
}

target "sandbox" {
    context = "."
    dockerfile = "sandbox/Dockerfile"
    tags = [
        "ultimate-coders/sandbox:latest",
        "ultimate-coders/sandbox:0.1.0",
    ]
    platforms = [
        "linux/amd64",
        "linux/arm64",
    ]
}
