mod cli;
mod commands;
mod mcp_server;
mod tools;

use clap::Parser;
use cli::{
    AuthLoginArgs, AuthSubcommands, Cli, Commands, DebugStartArgs, DebugSubcommands,
    McpSubcommands, OtaSubcommands, PiSubcommands, ProvisionSubcommands,
};

fn main() {
    let cli = Cli::parse();

    // `nff mcp` runs (or manages) the long-lived server rather than a discrete one-shot command,
    // so it never counts toward or shows the periodic nudge. (matches! borrows, so `cli.command`
    // is still available for the dispatch below.)
    let skip_nudge = matches!(cli.command, Commands::Mcp(_));

    let result = match cli.command {
        Commands::Init(args) => commands::init::run(&args),
        Commands::Compile(args) => commands::compile::run(&args),
        Commands::Flash(args) => commands::flash::run(&args),
        Commands::Monitor(args) => commands::monitor::run(&args),
        Commands::Doctor => commands::doctor::run(),
        Commands::Status => commands::status::run(),
        Commands::Clean => commands::clean::run(),
        Commands::Test => {
            // The test suite is a development-only command; the shipped binary carries
            // no Python runtime to delegate to.
            eprintln!("`nff test` is a development-only command and isn't available in this binary.");
            std::process::exit(2);
        }
        Commands::Connect => commands::connect::run(),
        Commands::Ota(o) => match o.sub {
            OtaSubcommands::Deploy(args) => commands::ota::run_deploy(&args),
            OtaSubcommands::Status(args) => commands::ota::run_status(&args),
            OtaSubcommands::List => commands::ota::run_list(),
            OtaSubcommands::Devices => commands::ota::run_devices(),
        },
        Commands::Diagnose(args) => commands::diagnose::run(&args),
        Commands::Fleet(args) => commands::fleet::run(&args),
        Commands::InstallDeps(args) => commands::install_deps::run(&args),
        Commands::Mcp(args) => match args.sub {
            Some(McpSubcommands::Stop) => commands::mcp::run_stop(),
            Some(McpSubcommands::Restart) => commands::mcp::run_restart(&args.host, args.port),
            Some(McpSubcommands::Logs(ref a)) => commands::mcp::run_logs(a),
            // Bare `nff mcp` → start the server in the foreground.
            None => tokio::runtime::Runtime::new()
                .expect("failed to create tokio runtime")
                .block_on(commands::mcp::run(&args)),
        },
        Commands::Auth(a) => match a.sub {
            Some(AuthSubcommands::Login(args)) => commands::auth::run_login(&args),
            Some(AuthSubcommands::Logout(args)) => commands::auth::run_logout(&args),
            Some(AuthSubcommands::Status) => commands::auth::run_status(),
            // Bare `nff auth` → browser login (email/password default to None).
            None => commands::auth::run_login(&AuthLoginArgs::default()),
        },
        Commands::Deauth(args) => commands::auth::run_logout(&args),
        Commands::Repair(args) => commands::repair::run(&args),
        Commands::Provision(p) => match p.sub {
            ProvisionSubcommands::Batch(args) => commands::provision::run_batch(&args),
        },
        Commands::Agent(args) => commands::agent::run(&args),
        Commands::Pi(p) => match p.sub {
            PiSubcommands::Probe(args) => commands::pi::run_probe(&args),
        },
        Commands::Policy(args) => commands::policy::run(&args),
        Commands::Debug(d) => match d.sub {
            Some(DebugSubcommands::Check(args)) => commands::debug::run_check(&args),
            Some(DebugSubcommands::Start(args)) => commands::debug::run_start(&args),
            // Bare `nff debug` → start a session with the group-level flags.
            None => commands::debug::run_start(&DebugStartArgs {
                elf: d.elf,
                board: d.board,
                interface: d.interface,
            }),
        },
    };

    // Periodic "star the repo / go Pro" nudge — shown on stderr every Nth attended CLI run,
    // regardless of whether the command succeeded.
    tools::nudge::maybe_show_cli(skip_nudge);

    if let Err(e) = result {
        eprintln!("error: {e}");
        std::process::exit(1);
    }
}
