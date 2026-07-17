if [ -f /etc/bash.bashrc ]; then
    . /etc/bash.bashrc
fi

# A bare exit finishes an exploratory shell successfully. Explicit codes survive.
exit() {
    if [ "$#" -eq 0 ]; then
        builtin exit 0
    fi
    builtin exit "$@"
}
