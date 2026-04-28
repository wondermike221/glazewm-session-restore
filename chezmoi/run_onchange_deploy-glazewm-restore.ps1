# Drop this file into the ROOT of your chezmoi source directory
# (i.e. ~/.local/share/chezmoi/run_onchange_deploy-glazewm-restore.ps1).
#
# chezmoi runs this script whenever its content changes, AFTER applying
# all managed files and externals — so the repo is already up-to-date
# before this fires.
#
# The `run_onchange_` prefix means chezmoi tracks a hash of this file and
# only re-runs when the script itself is modified (e.g. you change the task
# name or install path below). To force a re-run, bump the version comment:
#   version: 1

& "$env:USERPROFILE\repos\glazewm-session-restore\deploy.ps1" -NonInteractive
