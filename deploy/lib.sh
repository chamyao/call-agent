# Shared helpers for the setup scripts.

# install_frp VERSION BINARY  ->  /usr/local/bin/BINARY (frps or frpc)
install_frp() {
  local version="$1" binary="$2" arch
  if [[ -x "/usr/local/bin/$binary" ]] && "/usr/local/bin/$binary" --version 2>/dev/null | grep -qx "$version"; then
    return
  fi
  case "$(uname -m)" in
    x86_64) arch=amd64 ;;
    aarch64|arm64) arch=arm64 ;;
    armv7l) arch=arm ;;
    *) echo "Unsupported CPU: $(uname -m)" >&2; exit 1 ;;
  esac
  local name="frp_${version}_linux_${arch}"
  local tmp
  tmp="$(mktemp -d)"
  curl -fsSL "https://github.com/fatedier/frp/releases/download/v${version}/${name}.tar.gz" \
    | tar -xz -C "$tmp"
  install -m 755 "$tmp/$name/$binary" "/usr/local/bin/$binary"
  rm -rf "$tmp"
}
