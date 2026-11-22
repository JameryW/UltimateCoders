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
            .trim()
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
    /// Create the guard. Disabled hosts return an inactive guard; non-cgroup2
    /// paths fail explicitly and clean up the created directory rather than
    /// writing plain files into an ordinary directory.
    pub fn create(label: &str) -> io::Result<Self> {
        if !cgroup_enabled() {
            return Ok(Self {
                path: PathBuf::new(),
                active: false,
            });
        }
        let safe: String = label
            .chars()
            .take(64)
            .map(|ch| {
                if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                    ch
                } else {
                    '-'
                }
            })
            .collect();
        let prefix = if safe.is_empty() { "sandbox" } else { &safe };
        let unique = uuid::Uuid::new_v4();
        let root = cgroup_root();
        let path = root.join(format!("{prefix}-{unique}"));
        fs::create_dir_all(&root)?;
        fs::create_dir(&path)?;
        // A real cgroup2fs kernel mount immediately populates `cgroup.procs`
        // inside every new cgroup directory. On a non-cgroup2 directory that
        // file does not exist — remove the empty dir and refuse rather than
        // failing open and creating regular files on disk.
        if !path.join("cgroup.procs").exists() {
            let _ = fs::remove_dir(&path);
            return Err(io::Error::new(
                io::ErrorKind::Unsupported,
                format!("{} is not a cgroup v2 filesystem", root.display()),
            ));
        }
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

    /// Kill every process in the cgroup. Uses `cgroup.kill` on Linux 5.14+
    /// kernels and falls back to reading `cgroup.procs` + `SIGKILL` when
    /// `cgroup.kill` is absent on older cgroup v2 kernels.
    pub fn kill(&self) -> io::Result<()> {
        if !self.active {
            return Ok(());
        }
        match fs::write(self.path.join("cgroup.kill"), "1") {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => self.kill_procs_fallback(),
            Err(error) => Err(error),
        }
    }

    fn kill_procs_fallback(&self) -> io::Result<()> {
        #[cfg(target_os = "linux")]
        {
            let content = fs::read_to_string(self.path.join("cgroup.procs"))?;
            for line in content.lines() {
                if let Ok(pid) = line.trim().parse::<i32>() {
                    if pid > 0 {
                        let _ = std::process::Command::new("kill")
                            .args(["-KILL", &pid.to_string()])
                            .status();
                    }
                }
            }
            Ok(())
        }
        #[cfg(not(target_os = "linux"))]
        {
            Err(io::Error::new(
                io::ErrorKind::Unsupported,
                "cgroup.kill unavailable",
            ))
        }
    }

    /// Kill and remove the cgroup directory, retrying briefly while the kernel
    /// drains dying processes (`EBUSY`). Missing directory is harmless.
    pub fn cleanup(&mut self) -> io::Result<()> {
        if !self.active {
            return Ok(());
        }
        let _ = self.kill();
        let mut last_err = None;
        for attempt in 0..20 {
            match fs::remove_dir(&self.path) {
                Ok(()) => {
                    self.active = false;
                    return Ok(());
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {
                    self.active = false;
                    return Ok(());
                }
                Err(error) => {
                    last_err = Some(error);
                    if attempt < 19 {
                        let _ = self.kill();
                        std::thread::sleep(std::time::Duration::from_millis(10));
                    }
                }
            }
        }
        Err(last_err.unwrap_or_else(|| io::Error::other("failed to remove cgroup directory")))
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
    use std::sync::Mutex;
    use std::time::{Duration, Instant};

    static ENV_LOCK: Mutex<()> = Mutex::new(());

    #[test]
    fn guard_is_inactive_when_flag_is_off() {
        let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let old = std::env::var_os(CGROUP_ENV);
        std::env::remove_var(CGROUP_ENV);
        let guard = CgroupGuard::create("test").expect("inactive guard is infallible");
        assert!(!guard.is_active());
        if let Some(value) = old {
            std::env::set_var(CGROUP_ENV, value);
        }
    }

    #[test]
    fn create_rejects_non_cgroup2_directory_and_cleans_up() {
        let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let old_flag = std::env::var_os(CGROUP_ENV);
        let old_root = std::env::var_os(CGROUP_ROOT_ENV);
        let tmp_root =
            std::env::temp_dir().join(format!("uc-non-cgroup2-{}", uuid::Uuid::new_v4()));
        std::env::set_var(CGROUP_ENV, "true");
        std::env::set_var(CGROUP_ROOT_ENV, &tmp_root);
        let res = CgroupGuard::create("not-a-cgroup");
        assert!(res.is_err(), "non-cgroup2 directory must not fail open");
        let remaining = fs::read_dir(&tmp_root).map(|it| it.count()).unwrap_or(0);
        assert_eq!(remaining, 0, "failed create must clean up subdirectory");
        let _ = fs::remove_dir_all(&tmp_root);
        match old_flag {
            Some(v) => std::env::set_var(CGROUP_ENV, v),
            None => std::env::remove_var(CGROUP_ENV),
        }
        match old_root {
            Some(v) => std::env::set_var(CGROUP_ROOT_ENV, v),
            None => std::env::remove_var(CGROUP_ROOT_ENV),
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn cgroup_guard_kills_attached_process() {
        let _lock = ENV_LOCK.lock().unwrap_or_else(|e| e.into_inner());
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
