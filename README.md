# tgproxy

A local socks5 endpoint for Telegram on macOS. Telegram connects to `socks5://127.0.0.1:1080`, tgproxy forwards everything to an upstream HTTP or socks5 proxy with [gost](https://github.com/go-gost/gost). Optionally the upstream connection is wrapped in an ssh port forward, which hides the Telegram traffic from networks that inspect plaintext proxy tunnels and throttle MTProto inside them.

## Usage

* `tgproxy on`: load the launchd agent and show its progress live (agent start, ssh tunnel attempts and ssh errors, fallback to the direct upstream, gost errors), each step with the elapsed time and, on a terminal, a spinner naming the current step. It returns as soon as the socks5 endpoint listens, or after 90 seconds with a note that starting continues in the background; Ctrl+C stops only the watching. When the agent is already starting, it follows the progress instead of loading it again.
* `tgproxy off`: unload the agent and show the shutdown live. The agent reacts to the stop request at once (its waits run as background jobs, so the signal is not deferred), stops gost and the ssh tunnel and logs `stopped`.
* `tgproxy restart`: stop the agent if it runs, then start it with the current configuration.
* `tgproxy refresh`: restart the agent only if it is outdated, that is, if the program path launchd has loaded differs from `ProgramArguments` in `~/.config/tgproxy/agent.plist`. launchd keeps the definition it loaded and relaunches it after every exit, so after a configuration switch an agent loaded earlier keeps running the previous version. Home Manager activation runs `tgproxy refresh` after linking the new configuration, so a switch that changes the agent restarts a running one (a short Telegram reconnect) and leaves an up-to-date one alone. `tgproxy on` also restarts an outdated agent and `tgproxy status` warns about one.
* `tgproxy status`: when the endpoint listens, show the endpoint, the upstream route (through the ssh tunnel, or directly with the time until the next tunnel retry), uptime of the current route, the number of client connections and the PID (exit code 0); while starting, show the last step (exit code 1).
* `tgproxy logs`: show the last 50 log lines without gost's per-connection records, followed by the current time and status, so a quiet log of a healthy proxy is not mistaken for a stuck one.
* `tgproxy logs --follow`: follow the complete log.

The agent records its current route in `~/.local/state/tgproxy/agent.state` for `tgproxy status`.

The agent is loaded manually, it does not start at login. launchd restarts it when gost or the ssh tunnel exits. When the ssh tunnel cannot be established, the agent forwards directly to the upstream proxy instead, so Telegram keeps working on networks that block the ssh host but not the proxy, and logs that the traffic is not wrapped in ssh. Meanwhile it keeps retrying the tunnel with exponential backoff (5s doubling up to 5m), so an unreachable or rate-limiting ssh server is not hammered with connections, and switches back to the tunnelled upstream as soon as the tunnel listens.

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
