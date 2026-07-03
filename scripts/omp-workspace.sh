#!/usr/bin/env bash
# Shared OMP workspace setup — sourced by run-omp.sh and run-cluster.sh.
#
# OMP workspace root = uc.repos.yaml workspace_id=aiworks (~/aiworks). OMP
# launches with cwd here so the UI displays the configured workspace, not
# vendor/oh-my-pi. OMP's project-config discovery is single-cwd (getProjectDir,
# walks UP only) — it can't see nested UltimateCoders/.claude or vendor/.omp.
# setup_omp_workspace symlinks those nested configs up into ~/aiworks so OMP's
# fs.realpathSync-based discovery follows them. See .trellis research:
# omp-config-dir-injection.md.
#
# Expects: SCRIPT_DIR set by caller. Exports: OMP_WORKSPACE, OMP_VENDOR,
# OMP_ENTRY, UC_EXT, and functions link_config / setup_omp_workspace.

OMP_WORKSPACE="${OMP_WORKSPACE:-$HOME/aiworks}"
OMP_VENDOR="$SCRIPT_DIR/vendor/oh-my-pi"
OMP_ENTRY="$OMP_VENDOR/packages/coding-agent/src/cli.ts"
UC_EXT="$SCRIPT_DIR/packages/uc-orchestrator"

# Idempotent: link $2 -> $1 (target -> linkpath). Recreates if stale/wrong.
# Refuses to clobber a real (non-symlink) file/dir at linkpath — warns + skips.
link_config() {
    local target="$1" linkpath="$2"
    if [ -e "$linkpath" ] && [ ! -L "$linkpath" ]; then
        echo ">>> warn: $linkpath exists (not a symlink), skipping" >&2
        return 0
    fi
    if [ -L "$linkpath" ]; then
        # Already a symlink — refresh only if pointing elsewhere
        [ "$(readlink "$linkpath")" = "$target" ] && return 0
        rm -f "$linkpath"
    elif [ -e "$linkpath" ]; then
        rm -f "$linkpath"
    fi
    ln -sfn "$target" "$linkpath"
}

# Expose nested OMP project configs (.claude from UltimateCoders, .omp + .uc
# from vendor/oh-my-pi) up into OMP_WORKSPACE so single-cwd discovery finds them.
setup_omp_workspace() {
    mkdir -p "$OMP_WORKSPACE/.claude" "$OMP_WORKSPACE/.omp"
    # .claude — UltimateCoders repo-level config (agents/commands/hooks/skills/settings)
    local uc_claude="$SCRIPT_DIR/.claude"
    [ -e "$uc_claude/agents" ]    && link_config "$uc_claude/agents"    "$OMP_WORKSPACE/.claude/agents"
    [ -e "$uc_claude/commands" ]  && link_config "$uc_claude/commands"  "$OMP_WORKSPACE/.claude/commands"
    [ -e "$uc_claude/hooks" ]     && link_config "$uc_claude/hooks"     "$OMP_WORKSPACE/.claude/hooks"
    [ -e "$uc_claude/skills" ]    && link_config "$uc_claude/skills"    "$OMP_WORKSPACE/.claude/skills"
    [ -e "$uc_claude/settings.json" ] && link_config "$uc_claude/settings.json" "$OMP_WORKSPACE/.claude/settings.json"
    # .omp — vendor/oh-my-pi OMP-native config (commands/skills)
    local vendor_omp="$OMP_VENDOR/.omp"
    [ -e "$vendor_omp/commands" ] && link_config "$vendor_omp/commands" "$OMP_WORKSPACE/.omp/commands"
    [ -e "$vendor_omp/skills" ]   && link_config "$vendor_omp/skills"   "$OMP_WORKSPACE/.omp/skills"
    # .uc — uc-orchestrator task state (tasks/checkpoints). Previously lived at
    # vendor/oh-my-pi/.uc when cwd was vendor; symlink so existing state follows
    # the cwd move to OMP_WORKSPACE (TaskStore uses <cwd>/.uc/{tasks,checkpoints}).
    [ -e "$OMP_VENDOR/.uc" ] && link_config "$OMP_VENDOR/.uc" "$OMP_WORKSPACE/.uc"
}
