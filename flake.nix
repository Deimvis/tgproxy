{
  description = "Local socks5 endpoint for Telegram that forwards to an upstream proxy, optionally through an ssh tunnel";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      homeManagerModules.default = import ./nix/home-manager-module.nix;

      checks = forAllSystems (pkgs: {
        unit = pkgs.runCommand "tgproxy-unit-tests" {
          nativeBuildInputs = [ pkgs.python3 pkgs.bash pkgs.shellcheck pkgs.coreutils pkgs.gnused ];
        } ''
          export HOME="$TMPDIR"
          cp -r ${./.} source
          chmod -R u+w source
          cd source
          python3 -m unittest discover -s tests -v
          touch "$out"
        '';
      });

      devShells = forAllSystems (pkgs: {
        default = pkgs.mkShell {
          packages = [ pkgs.python3 pkgs.shellcheck ];
        };
      });
    };
}
