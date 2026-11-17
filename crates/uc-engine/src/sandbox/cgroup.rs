//! Linux cgroup v2 process-group guard for the subprocess sandbox (#24).
//!
//! This is deliberately small and opt-in: [`CgroupGuard::create`] returns an
//! inactive guard unless `UC_RL_CGROUP` is truthy.  When active it creates a
//! dedicated cgroup v2 subtree, attaches the spawned command's PID, and uses
//! `cgroup.kill` on drop so descendants cannot escape the rollout.
//!
//! The guard does not create a PID namespace itself; it is the runtime half of
//! the LEGO-RL hardening story.  `RlHardening::prepare_process_group` remains
//! the declarative supervisor plan used by environments that provide namespace
//! creation out of band.

use std::fs;
use std::io;
use std::path::PathBuf;

pub const CGROUP_ENV: &str = "UC_RL_CGROUP";
pub const CGROUP_ROOT_ENV: &str = "UC_RL_CGROUP_ROOT";
const DEFAULT_CGROUP_ROOT: &str = "/sys/fs/cgroup/uc-rl";

/// Whether the runtime cgroup guard is enabled.
pub fn cgroup_enabled() -> bool {
    matches!(
        std::env::var(CGROUP_ENV)
            .unwrap_or_default()
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

fn cgroup_root() -> PathBuf {
    std::env::var_os(CGROUP_ROOT_ENV)
        .map(PathBuf::from)
        .filter(|path| !path.as_os_str().is_empty())
        .unwrap_or_else(|| PathBuf::from(DEFAULT_CGROUP_ROOT))
}

/// A cgroup v2 directory owned by one sandbox command.
pub struct CgroupGuard {
    path: PathBuf,
    active: bool,
}

impl CgroupGuard {
    /// Create the guard.  Disabled or unavailable cgroup v2 hosts return an
    /// inactive guard rather than failing the command.
    pub fn create(label: &str) -> io::Result<Self> {
        if !cgroup_enabled() {
            return Ok(Self {
                path: PathBuf::new(),
                active: false,
            });
        }
        let safe: String = label
            .chars()
            .map(|ch| {
                if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                    ch
                } else {
                    '-'
                }
            })
            .collect();
        let unique = uuid::Uuid::new_v4();
        let path = cgroup_root().join(format!("{safe}-{unique}"));
        fs::create_dir_all(cgroup_root())?;
        fs::create_dir(&path)?;
        Ok(Self { path, active: true })
    }

    pub fn is_active(&self) -> bool {
        self.active
    }

    pub fn path(&self) -> &std::path::Path {
        &self.path
    }

    /// Move one process into this cgroup.
    pub fn attach(&self, pid: u32) -> io::Result<()> {
        if !self.active {
            return Ok(());
        }
        fs::write(self.path.join("cgroup.procs"), pid.to_string())
    }

    /// Kill every process in the cgroup.  `cgroup.kill` exists on cgroup v2
    /// kernels; an older kernel returns an error and the caller can fall back
    /// to the ordinary child kill.
    pub fn kill(&self) -> io::Result<()> {
        if !self.active {
            return Ok(());
        }
        fs::write(self.path.join("cgroup.kill"), "1")
    }

    /// Kill and remove the cgroup directory.  Missing directory is harmless.
    pub fn cleanup(&mut self) -> io::Result<()> {
        if !self.active {
            return Ok(());
        }
        let _ = self.kill();
        let result = fs::remove_dir(&self.path);
        self.active = false;
        match result {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error),
        }
    }
}

impl Drop for CgroupGuard {
    fn drop(&mut self) {
        let _ = self.cleanup();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};

    #[test]
    fn guard_is_inactive_when_flag_is_off() {
        let old = std::env::var_os(CGROUP_ENV);
        std::env::remove_var(CGROUP_ENV);
        let guard = CgroupGuard::create("test").expect("inactive guard is infallible");
        assert!(!guard.is_active());
        if let Some(value) = old {
            std::env::set_var(CGROUP_ENV, value);
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn cgroup_guard_kills_attached_process() {
        let old = std::env::var_os(CGROUP_ENV);
        std::env::set_var(CGROUP_ENV, "1");
        let mut child = match Command::new("sh")
            .args(["-c", "sleep 30"])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
        {
            Ok(child) => child,
            Err(_) => {
                if let Some(value) = old {
                    std::env::set_var(CGROUP_ENV, value);
                } else {
                    std::env::remove_var(CGROUP_ENV);
                }
                return;
            }
        };
        let mut guard = match CgroupGuard::create("test-kill") {
            Ok(guard) if guard.is_active() => guard,
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                if let Some(value) = old {
                    std::env::set_var(CGROUP_ENV, value);
                } else {
                    std::env::remove_var(CGROUP_ENV);
                }
                return;
            }
        };
        guard.attach(child.id()).expect("attach child to cgroup");
        let started = Instant::now();
        guard.kill().expect("cgroup kill should be available");
        let status = child.wait().expect("child should exit after cgroup kill");
        let elapsed = started.elapsed();
        assert!(
            !status.success(),
            "cgroup.kill must terminate the child, got {status:?}"
        );
        assert!(
            elapsed < Duration::from_secs(5),
            "cgroup kill should not wait for the full sleep"
        );
        let _ = guard.cleanup();
        if let Some(value) = old {
            std::env::set_var(CGROUP_ENV, value);
        } else {
            std::env::remove_var(CGROUP_ENV);
        }
    }
}
