#!/bin/sh
conf="${1:-/etc/wsl.conf}"
boot_command='command = umask 022; readlink /proc/self/ns/pid >/run/factory-host-pidns'
if [ -f "$conf" ] && grep -qxF "$boot_command" "$conf"; then
    echo "already set: ${conf}"
    exit 0
fi
src=/dev/null
[ -f "$conf" ] && src="$conf"
existing="$(awk '/^[[:space:]]*\[/ { boot = /^[[:space:]]*\[boot\][[:space:]]*$/ }
    boot && /^[[:space:]]*command[[:space:]]*=/ { print; exit }' "$src")"
if [ -n "$existing" ]; then
    printf '%s\n%s\nmerge by hand: join both with ;\n' "$existing" "$boot_command" >&2
    exit 1
fi
tmp="$(mktemp "${conf}.XXXXXX")" || exit 1
if [ "$src" = "$conf" ]; then
    cp -p "$conf" "$tmp"
else
    chmod 0644 "$tmp"
fi &&
    awk -v line="$boot_command" '{ print }
        !done && /^[[:space:]]*\[boot\][[:space:]]*$/ { print line; done = 1 }
        END { if (!done) { if (NR) print ""; print "[boot]"; print line } }' "$src" >"$tmp" &&
    mv -f "$tmp" "$conf" || { rm -f "$tmp"; exit 1; }
echo "added to ${conf}: ${boot_command}"
echo "next: restart WSL (wsl --shutdown from Windows), then check: cat /run/factory-host-pidns equals readlink /proc/self/ns/pid in a host shell"
