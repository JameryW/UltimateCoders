//! cargo run -p uc-engine --no-default-features --features storage --example runtime_report -- GRAPH_ID

#[cfg(feature = "storage")]
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args().skip(1);
    let graph_id = args
        .next()
        .ok_or("usage: runtime_report GRAPH_ID (UC_DATABASE_URL required)")?;
    if graph_id.trim().is_empty() || args.next().is_some() {
        return Err("usage: runtime_report GRAPH_ID (UC_DATABASE_URL required)".into());
    }
    let url = std::env::var("UC_DATABASE_URL")
        .map_err(|_| "usage: runtime_report GRAPH_ID (UC_DATABASE_URL required)")?;
    // GraphStore::connect runs migrations; diagnostics must not call it.
    let pool = sqlx::postgres::PgPoolOptions::new()
        .max_connections(1)
        .acquire_timeout(std::time::Duration::from_secs(10))
        .connect(&url)
        .await?;
    let report = uc_engine::runtime_metrics::read_runtime_report(&pool, &graph_id).await?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}

#[cfg(not(feature = "storage"))]
fn main() {
    eprintln!("runtime_report requires --features storage");
    std::process::exit(2);
}
