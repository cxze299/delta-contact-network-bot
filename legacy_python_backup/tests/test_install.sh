#!/bin/sh
# Offline installation smoke test; all Docker and network calls are fake.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
mkdir -p "$tmp/project/scripts" "$tmp/bin"
cp "$root/install.sh" "$tmp/project/"
cp "$root/scripts/install-interactive.sh" "$root/scripts/deploy-server.sh" "$tmp/project/scripts/"
cp "$root/compose.yaml" "$root/Dockerfile" "$tmp/project/"
cat > "$tmp/bin/uname" <<'EOF'
#!/bin/sh
case "$1" in -s) echo Linux ;; -m) echo x86_64 ;; esac
EOF
cat > "$tmp/bin/docker" <<'EOF'
#!/bin/sh
case "$1" in
 info|logs|image) exit 0 ;;
 inspect) case "$*" in *--format*) echo running; exit 0 ;; *) exit 1 ;; esac ;;
 exec) echo 0.7.9; exit 0 ;;
 compose)
  case "$*" in
   *--entrypoint*) cat >/dev/null; printf '250000:%s:%s\n' 0123456789abcdef0123456789abcdef 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef ;;
   *configure*) if [ "${TEST_CONFIG_FAIL:-0}" = 1 ]; then exit 1; fi; echo 'Test account configured' ;;
  esac
  exit 0 ;;
esac
exit 1
EOF
cat > "$tmp/bin/curl" <<'EOF'
#!/bin/sh
while [ "$#" -gt 0 ]; do
 case "$1" in -o) shift; case "$1" in /dev/null) ;; *) printf 'test rpc' > "$1" ;; esac ;; -w) shift; printf 401 ;; esac
 shift
done
EOF
cat > "$tmp/bin/sha256sum" <<'EOF'
#!/bin/sh
echo 'b73ce0f8732f7589cd34e59db4b2ed6a0f6ab6857e691b73b06710e150af4ee0  fake'
EOF
chmod +x "$tmp/bin/"* "$tmp/project/install.sh" "$tmp/project/scripts/"*.sh
PATH="$tmp/bin:$PATH"; export PATH
cd "$tmp/project"
printf '\ntest.example.org\nadmin@example.org\n1\n8787\n\nyes\n' | ./install.sh > "$tmp/output"
test -s .env
test -s admin-web-access.txt
grep -q '^CONTACTBOT_XDC_ADMIN_VERIFIER=250000:' .env
grep -q '^小程序管理员密钥：' admin-web-access.txt
grep -q '部署完成' "$tmp/output"
cp .env "$tmp/env-before"
if printf '\n' | ./install.sh > /dev/null 2>&1; then
 echo 'existing deployment was not protected' >&2; exit 1
fi
cmp .env "$tmp/env-before"
mkdir -p "$tmp/failing/scripts"
cp "$root/install.sh" "$tmp/failing/"
cp "$root/scripts/install-interactive.sh" "$root/scripts/deploy-server.sh" "$tmp/failing/scripts/"
cp "$root/compose.yaml" "$root/Dockerfile" "$tmp/failing/"
chmod +x "$tmp/failing/install.sh" "$tmp/failing/scripts/"*.sh
cd "$tmp/failing"
if printf '\ntest.example.org\nadmin@example.org\n1\n8787\n\nyes\n' | TEST_CONFIG_FAIL=1 ./install.sh > /dev/null 2>&1; then
 echo 'simulated registration failure was not reported' >&2; exit 1
fi
test -s .env
test -s admin-web-access.txt
grep -q '^CONTACTBOT_XDC_ADMIN_VERIFIER=250000:' .env
grep -q '^小程序管理员密钥：' admin-web-access.txt
echo 'installer smoke test passed'
