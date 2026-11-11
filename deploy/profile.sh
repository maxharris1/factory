# Source this from each factory bot's shell profile after deploy/install.sh.
if [ -r "${HOME}/.config/factory/profile.sh" ]; then
    . "${HOME}/.config/factory/profile.sh"
fi
