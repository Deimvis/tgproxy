# tgproxy

A local socks5 endpoint for Telegram on macOS. Telegram connects to `socks5://127.0.0.1:1080`, tgproxy forwards everything to an upstream HTTP or socks5 proxy with [gost](https://github.com/go-gost/gost). Optionally the upstream connection is wrapped in an ssh port forward, which hides the Telegram traffic from networks that inspect plaintext proxy tunnels and throttle MTProto inside them.

## Usage

* `tgproxy on`: load the launchd agent and wait until the socks5 endpoint listens.
* `tgproxy off`: unload the agent.
* `tgproxy status`: report whether the endpoint is listening (exit code 0 when it is).
* `tgproxy logs`: show the last 50 lines of `~/Library/Logs/tgproxy.log`.

The agent is loaded manually, it does not start at login. launchd restarts it when gost or the ssh tunnel exits.

## Nix

The flake exposes a home-manager module `homeManagerModules.default`:

```nix
services.tgproxy = {
  enable = true;
  listenAddress = "127.0.0.1:1080";                          # default
  upstreamProxyUrl = "http://username:password@proxy.example.com:3128";
  sshTunnel = {
    enable = true;
    host = "jump";                                            # ~/.ssh/config alias or user@host, key auth
    localPort = 15432;                                        # default
  };
};
```

With `sshTunnel.enable = true` the agent runs `ssh -N -L <localPort>:<proxy host>:<proxy port> <host>` and points gost at the forwarded port, so the ssh host must be able to reach the upstream proxy. The host key must already be in `~/.ssh/known_hosts` and the key must not require a passphrase prompt, since ssh runs in batch mode under launchd.

Add the flake as an input and import the module into your home-manager configuration:

```nix
inputs.tgproxy.url = "github:Deimvis/tgproxy";
inputs.tgproxy.inputs.nixpkgs.follows = "nixpkgs";

home-manager.users.<name>.imports = [ inputs.tgproxy.homeManagerModules.default ];
```

## Development

```
make test       # python unit tests for the shell scripts (needs bash 5.1+, shellcheck)
make test-nix   # module evaluation tests
make check      # both, in the nix sandbox
```
