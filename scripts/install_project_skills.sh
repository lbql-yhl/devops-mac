#!/bin/zsh
set -euo pipefail

project_root="${0:A:h:h}"
canonical_root="$project_root/skills"
codex_root="${CODEX_HOME:-$HOME/.codex}"
install_root="${CODEX_SKILLS_DIR:-$codex_root/skills}"
mode="${1:---install}"
backup_root=""
install_committed=0
typeset -a moved_skills
typeset -a created_skills

skills=(
  utm-vm-clone utm-notion utm-clash-ip utm-login utm-edit utm-key utm-apps utm-business
  utm-env utm-script utm-image utm-p8 utm-21 utm-22 utm-23 utm-24
)
typeset -a standalone_skills
standalone_skills=()
removed_skills=(utm-a notion-utm notion-utm-1 utm-clone-macos utm-1 utm-2 utm-3 vm-down utm-4 utm-5 files utm-clash utm-6 utm-10 utm-11 utm-12 utm-13 utm-14 utm-15 utm-16 utm-17 utm-19 utm-20)
typeset -a install_entries
install_entries=("${skills[@]}" "${standalone_skills[@]}" "_shared")

if [[ "$mode" != "--install" && "$mode" != "--check" ]]; then
  print -u2 -- "usage: $0 [--install|--check]"
  exit 2
fi

validate_all_sources() {
  [[ -d "$canonical_root" && ! -L "$canonical_root" ]] || {
    print -u2 -- "unsafe canonical root: $canonical_root"
    return 3
  }
  local canonical_real="${canonical_root:A}"
  local source_dir
  local skill
  for skill in "${skills[@]}"; do
    source_dir="$canonical_root/$skill"
    [[ -d "$source_dir" && ! -L "$source_dir" && -f "$source_dir/SKILL.md" ]] || {
      print -u2 -- "missing or linked canonical skill: $skill"
      return 3
    }
    [[ "${source_dir:h:A}" == "$canonical_real" && "${source_dir:t}" == "$skill" ]] || {
      print -u2 -- "canonical skill escaped root: $skill"
      return 3
    }
  done
  [[ ${#skills[@]} -eq 16 ]] || { print -u2 -- "canonical skill count is not 16"; return 3; }
  for skill in "${standalone_skills[@]}"; do
    source_dir="$canonical_root/$skill"
    [[ -d "$source_dir" && ! -L "$source_dir" && -f "$source_dir/SKILL.md" ]] || {
      print -u2 -- "missing or linked standalone skill: $skill"
      return 3
    }
    [[ "${source_dir:h:A}" == "$canonical_real" && "${source_dir:t}" == "$skill" ]] || {
      print -u2 -- "standalone skill escaped root: $skill"
      return 3
    }
  done
  [[ ${#standalone_skills[@]} -eq 0 ]] || { print -u2 -- "standalone skill count is not 0"; return 3; }
  [[ -d "$canonical_root/_shared" && ! -L "$canonical_root/_shared" \
      && -f "$canonical_root/_shared/AUTOMATION_CONTRACT.md" ]] || {
    print -u2 -- "missing canonical shared contract"
    return 3
  }
}

validate_install_root() {
  local root_real="${install_root:A}"
  local project_real="${project_root:A}"
  local canonical_real="${canonical_root:A}"
  [[ ! -L "$install_root" ]] || { print -u2 -- "unsafe install root symlink: $install_root"; return 5; }
  [[ "$root_real" != "/" && "$root_real" != "${HOME:A}" ]] || {
    print -u2 -- "unsafe install root: $install_root"
    return 5
  }
  [[ "$root_real" != "$project_real" && "$root_real" != "$canonical_real" ]] || {
    print -u2 -- "unsafe install root overlaps project: $install_root"
    return 5
  }
}

rollback_install() {
  local exit_code=$?
  trap - EXIT INT TERM
  if (( install_committed == 0 )); then
    local skill
    for skill in "${created_skills[@]}"; do
      [[ -L "$install_root/$skill" ]] && /bin/rm -- "$install_root/$skill"
    done
    for skill in "${moved_skills[@]}"; do
      if [[ -e "$backup_root/$skill" || -L "$backup_root/$skill" ]]; then
        /bin/mv -- "$backup_root/$skill" "$install_root/$skill"
      fi
    done
  fi
  if [[ -n "$backup_root" && -d "$backup_root" ]]; then
    [[ "${backup_root:h:A}" == "${install_root:A}" && "${backup_root:t}" == .project-skill-backup.* ]] || {
      print -u2 -- "unsafe backup cleanup refused: $backup_root"
      exit 7
    }
    /bin/rm -rf -- "$backup_root"
  fi
  exit "$exit_code"
}

validate_all_sources
python3 "$project_root/scripts/preflight.py" --project-only

if [[ "$mode" == "--check" ]]; then
  [[ -d "$install_root" ]] || { print -u2 -- "missing install root: $install_root"; exit 4; }
  validate_install_root
  for skill in "${install_entries[@]}"; do
    target="$install_root/$skill"
    [[ -L "$target" ]] || { print -u2 -- "not linked: $target"; exit 4; }
    [[ "${target:A}" == "${canonical_root:A}/$skill" ]] || { print -u2 -- "wrong link: $target"; exit 4; }
  done
  for skill in "${removed_skills[@]}"; do
    target="$install_root/$skill"
    [[ ! -e "$target" && ! -L "$target" ]] || { print -u2 -- "removed skill still installed: $target"; exit 4; }
  done
  print -- "PROJECT_SKILLS_INSTALLED=16"
  print -- "PROJECT_STANDALONE_SKILLS_INSTALLED=0"
  print -- "PROJECT_SHARED_CONTRACT=linked"
  print -- "PROJECT_SKILLS_ROOT=$canonical_root"
  print -- "CODEX_SKILLS_ROOT=$install_root"
  exit 0
fi

mkdir -p "$install_root"
validate_install_root
backup_root="$(mktemp -d "$install_root/.project-skill-backup.XXXXXX")"
trap rollback_install EXIT INT TERM

for skill in "${removed_skills[@]}"; do
  target="$install_root/$skill"
  if [[ -L "$target" ]]; then
    link_target="$(readlink "$target")"
    [[ "$link_target" == "$canonical_root/$skill" ]] || {
      print -u2 -- "refusing to remove unrelated skill link: $target -> $link_target"
      exit 5
    }
    /bin/mv -- "$target" "$backup_root/$skill"
    moved_skills+=("$skill")
  elif [[ -e "$target" ]]; then
    print -u2 -- "refusing to remove non-link skill: $target"
    exit 5
  fi
done

for skill in "${install_entries[@]}"; do
  source_dir="$canonical_root/$skill"
  target="$install_root/$skill"
  [[ "${target:h:A}" == "${install_root:A}" && "${target:t}" == "$skill" ]] || {
    print -u2 -- "unsafe target: $target"
    exit 5
  }
  if [[ -e "$target" || -L "$target" ]]; then
    /bin/mv -- "$target" "$backup_root/$skill"
    moved_skills+=("$skill")
  fi
  /bin/ln -s "$source_dir" "$target"
  created_skills+=("$skill")
  [[ "${target:A}" == "${source_dir:A}" ]] || { print -u2 -- "link verification failed: $skill"; exit 6; }
done

for skill in "${install_entries[@]}"; do
  target="$install_root/$skill"
  source_dir="$canonical_root/$skill"
  [[ -L "$target" && "${target:A}" == "${source_dir:A}" ]] || {
    print -u2 -- "final link verification failed: $skill"
    exit 6
  }
done

install_committed=1
trap - EXIT INT TERM
[[ "${backup_root:h:A}" == "${install_root:A}" && "${backup_root:t}" == .project-skill-backup.* ]] || {
  print -u2 -- "unsafe backup cleanup refused: $backup_root"
  exit 7
}
/bin/rm -rf -- "$backup_root"

print -- "PROJECT_SKILLS_INSTALLED=16"
print -- "PROJECT_STANDALONE_SKILLS_INSTALLED=0"
print -- "PROJECT_SHARED_CONTRACT=linked"
print -- "PROJECT_SKILLS_ROOT=$canonical_root"
print -- "CODEX_SKILLS_ROOT=$install_root"
