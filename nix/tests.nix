let
  lib = {
    mkEnableOption = name: { type = "bool"; inherit name; };
    mkOption = attrs: attrs;
    mkIf = condition: value: if condition then value else {};
    types = { str = "str"; port = "port"; };
    generators.toPlist = options: value: assert options.escape; value;
  };
  pkgs = {
    coreutils = "/test/coreutils";
    gnused = "/test/gnused";
    gost = "/test/gost";
    writeShellApplication = value: value // { outPath = "/test/${value.name}"; };
  };
  hasInfix = needle: haystack: builtins.replaceStrings [ needle ] [ "" ] haystack != haystack;
  hasPlaceholder = text: builtins.length (builtins.split "@[a-z_]+@" text) > 1;
  evaluate = services: import ./home-manager-module.nix {
    config = { home.homeDirectory = "/Users/test"; inherit services; };
    inherit lib pkgs;
  };
  tunnelled = evaluate {
    tgproxy = {
      enable = true;
      listenAddress = "127.0.0.1:1080";
      upstreamProxyUrl = "http://user:secret@203.0.113.7:3128";
      sshTunnel = { enable = true; host = "jump"; localPort = 15432; };
    };
  };
  direct = evaluate {
    tgproxy = {
      enable = true;
      listenAddress = "127.0.0.1:2080";
      upstreamProxyUrl = "socks5://203.0.113.7:1080";
      sshTunnel = { enable = false; host = ""; localPort = 15432; };
    };
  };
  disabled = evaluate {
    tgproxy = {
      enable = false;
      listenAddress = "127.0.0.1:1080";
      upstreamProxyUrl = "http://203.0.113.7:3128";
      sshTunnel = { enable = false; host = ""; localPort = 15432; };
    };
  };
  agentOf = module: module.config.home.file.".config/tgproxy/agent.plist".text;
  commandOf = module: builtins.head module.config.home.packages;
  agentScriptOf = module: builtins.elemAt module.config.home.packages 1;
in
assert hasPlaceholder "x @foo_bar@ y";
assert !hasPlaceholder "user:secret@127.0.0.1";
assert (agentOf tunnelled).Label == "org.nix-community.home.tgproxy";
assert (agentOf tunnelled).ProgramArguments == [ "/test/tgproxy-agent/bin/tgproxy-agent" ];
assert (agentOf tunnelled).KeepAlive;
assert !(agentOf tunnelled).RunAtLoad;
assert (agentOf tunnelled).ThrottleInterval == 5;
assert (agentOf tunnelled).StandardOutPath == "/Users/test/Library/Logs/tgproxy.log";
assert (agentOf tunnelled).StandardErrorPath == (agentOf tunnelled).StandardOutPath;
assert (commandOf tunnelled).name == "tgproxy";
assert !hasPlaceholder (commandOf tunnelled).text;
assert hasInfix "listen_address='127.0.0.1:1080'" (commandOf tunnelled).text;
assert hasInfix "/bin/launchctl bootstrap" (commandOf tunnelled).text;
assert hasInfix "listen_address='127.0.0.1:2080'" (commandOf direct).text;
assert (agentScriptOf tunnelled).name == "tgproxy-agent";
assert !hasPlaceholder (agentScriptOf tunnelled).text;
assert hasInfix "gost_upstream_url='http://user:secret@127.0.0.1:15432'" (agentScriptOf tunnelled).text;
assert hasInfix "tunnel_ssh_host='jump'" (agentScriptOf tunnelled).text;
assert hasInfix "direct_upstream_url='http://user:secret@203.0.113.7:3128'" (agentScriptOf tunnelled).text;
assert hasInfix "tunnel_target_address='203.0.113.7:3128'" (agentScriptOf tunnelled).text;
assert hasInfix "tunnel_local_port='15432'" (agentScriptOf tunnelled).text;
assert hasInfix "/test/gost/bin/gost -L" (agentScriptOf tunnelled).text;
assert hasInfix "gost_upstream_url='socks5://203.0.113.7:1080'" (agentScriptOf direct).text;
assert hasInfix "tunnel_ssh_host=''" (agentScriptOf direct).text;
assert disabled.config == {};
assert tunnelled.options.services.tgproxy.listenAddress.default == "127.0.0.1:1080";
assert tunnelled.options.services.tgproxy.sshTunnel.localPort.default == 15432;
true
