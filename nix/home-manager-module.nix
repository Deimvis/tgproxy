{ config, lib, pkgs, ... }:

let
  cfg = config.services.tgproxy;
  upstreamProxyUrlPattern = "^(https?|socks5)://(([^:@/]+):([^@/]+)@)?([^:@/]+):([0-9]+)$";
  upstreamProxyUrlParts =
    let parts = builtins.match upstreamProxyUrlPattern cfg.upstreamProxyUrl;
    in if parts == null
      then throw "services.tgproxy.upstreamProxyUrl must look like scheme://[username:password@]host:port, got ${cfg.upstreamProxyUrl}"
      else parts;
  upstreamProxyScheme = builtins.elemAt upstreamProxyUrlParts 0;
  upstreamProxyCredentialsPrefix =
    let prefix = builtins.elemAt upstreamProxyUrlParts 1;
    in if prefix == null then "" else prefix;
  upstreamProxyHost = builtins.elemAt upstreamProxyUrlParts 4;
  upstreamProxyPort = builtins.elemAt upstreamProxyUrlParts 5;
  tunnelLocalPort = toString cfg.sshTunnel.localPort;
  gostUpstreamUrl =
    if cfg.sshTunnel.enable
    then "${upstreamProxyScheme}://${upstreamProxyCredentialsPrefix}127.0.0.1:${tunnelLocalPort}"
    else cfg.upstreamProxyUrl;
  substitutePlaceholders = placeholders: text:
    builtins.replaceStrings
      (map (name: "@${name}@") (builtins.attrNames placeholders))
      (builtins.attrValues placeholders)
      text;
  command = pkgs.writeShellApplication {
    name = "tgproxy";
    text = substitutePlaceholders {
      listen_address = cfg.listenAddress;
      launchctl = "/bin/launchctl";
      lsof = "/usr/sbin/lsof";
      id = "${pkgs.coreutils}/bin/id";
      mkdir = "${pkgs.coreutils}/bin/mkdir";
      sleep = "${pkgs.coreutils}/bin/sleep";
      tail = "${pkgs.coreutils}/bin/tail";
      sed = "${pkgs.gnused}/bin/sed";
    } (builtins.readFile ../src/tgproxy.sh);
  };
  agent = pkgs.writeShellApplication {
    name = "tgproxy-agent";
    text = substitutePlaceholders {
      listen_address = cfg.listenAddress;
      gost_upstream_url = gostUpstreamUrl;
      direct_upstream_url = cfg.upstreamProxyUrl;
      tunnel_ssh_host = if cfg.sshTunnel.enable then cfg.sshTunnel.host else "";
      tunnel_target_address = "${upstreamProxyHost}:${upstreamProxyPort}";
      tunnel_local_port = tunnelLocalPort;
      ssh = "/usr/bin/ssh";
      lsof = "/usr/sbin/lsof";
      sleep = "${pkgs.coreutils}/bin/sleep";
      gost = "${pkgs.gost}/bin/gost";
    } (builtins.readFile ../src/agent.sh);
  };
  logFilePath = "${config.home.homeDirectory}/Library/Logs/tgproxy.log";
in
{
  options.services.tgproxy = {
    enable = lib.mkEnableOption "tgproxy, a local socks5 endpoint for Telegram forwarding to an upstream proxy";

    listenAddress = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1:1080";
      description = "Address the local socks5 endpoint listens on; configure it as a socks5 proxy in Telegram.";
    };

    upstreamProxyUrl = lib.mkOption {
      type = lib.types.str;
      description = "Upstream proxy in the form scheme://[username:password@]host:port, where scheme is http, https or socks5.";
    };

    sshTunnel = {
      enable = lib.mkEnableOption "wrapping the upstream proxy connection in an ssh port forward, hiding its contents from the local network";
      host = lib.mkOption {
        type = lib.types.str;
        description = "ssh destination (an alias from ~/.ssh/config or user@host) that can reach the upstream proxy; key-based authentication is required.";
      };
      localPort = lib.mkOption {
        type = lib.types.port;
        default = 15432;
        description = "Local port the ssh port forward listens on.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ command agent ];

    home.file.".config/tgproxy/agent.plist".text = lib.generators.toPlist { escape = true; } {
      Label = "org.nix-community.home.tgproxy";
      ProgramArguments = [ "${agent}/bin/tgproxy-agent" ];
      KeepAlive = true;
      RunAtLoad = false;
      ThrottleInterval = 5;
      StandardOutPath = logFilePath;
      StandardErrorPath = logFilePath;
    };
  };
}
