//! Subtask dependency resolution via topological sort (Kahn's algorithm).
//!
//! Produces execution layers: subtasks within a layer have no mutual
//! dependencies and can run in parallel.  Detects circular dependencies.

use uc_types::{EngineError, Subtask, TaskId};

/// Resolve subtask execution order using Kahn's algorithm.
///
/// Returns layers of subtask IDs.  Subtasks within the same layer
/// have no dependency on each other and may execute concurrently.
/// Layer N+1 depends on at least one subtask in layer N.
///
/// # Errors
///
/// Returns `EngineError::TaskError` if a circular dependency is detected
/// (not all subtasks could be placed into a layer).
pub fn resolve_execution_order(
    subtasks: &[Subtask],
) -> Result<Vec<Vec<TaskId>>, EngineError> {
    if subtasks.is_empty() {
        return Ok(vec![]);
    }

    // Build index: subtask_id → position
    let id_to_idx: std::collections::HashMap<TaskId, usize> = subtasks
        .iter()
        .enumerate()
        .map(|(i, s)| (s.id.clone(), i))
        .collect();

    let n = subtasks.len();
    let mut in_degree = vec![0usize; n];
    let mut adj: Vec<Vec<usize>> = vec![vec![]; n]; // adj[a] = indices that depend on a

    for (i, st) in subtasks.iter().enumerate() {
        for dep in &st.depends_on {
            if let Some(&dep_idx) = id_to_idx.get(dep) {
                adj[dep_idx].push(i);
                in_degree[i] += 1;
            }
            // Unknown dependency → ignore (might be from a different task)
        }
    }

    // BFS: collect nodes with in_degree 0 → one layer
    let mut layers = Vec::new();
    let mut visited = 0usize;
    let mut queue: Vec<usize> = (0..n).filter(|&i| in_degree[i] == 0).collect();

    while !queue.is_empty() {
        // Current layer = all nodes with in_degree 0
        let layer_ids: Vec<TaskId> = queue
            .iter()
            .map(|&i| subtasks[i].id.clone())
            .collect();
        visited += queue.len();
        layers.push(layer_ids);

        // Decrement in-degrees of dependents
        let mut next_queue = Vec::new();
        for &idx in &queue {
            for &dep_idx in &adj[idx] {
                in_degree[dep_idx] -= 1;
                if in_degree[dep_idx] == 0 {
                    next_queue.push(dep_idx);
                }
            }
        }
        queue = next_queue;
    }

    if visited != n {
        return Err(EngineError::TaskError(
            "circular dependency detected in subtasks".into(),
        ));
    }

    Ok(layers)
}

#[cfg(test)]
mod tests {
    use super::*;
    use uc_types::SubtaskStatus;

    fn make_subtask(id: &str, depends_on: Vec<&str>) -> Subtask {
        Subtask {
            id: TaskId(id.to_string()),
            parent_id: TaskId("T1".to_string()),
            description: id.to_string(),
            status: SubtaskStatus::Pending,
            assigned_worker: None,
            depends_on: depends_on.into_iter().map(|d| TaskId(d.to_string())).collect(),
            file_constraints: Vec::new(),
            expected_output: String::new(),
            result: None,
        }
    }

    #[test]
    fn empty_subtasks() {
        let result = resolve_execution_order(&[]);
        assert!(result.unwrap().is_empty());
    }

    #[test]
    fn single_no_deps() {
        let subs = vec![make_subtask("A", vec![])];
        let layers = resolve_execution_order(&subs).unwrap();
        assert_eq!(layers, vec![vec![TaskId("A".to_string())]]);
    }

    #[test]
    fn linear_chain() {
        // A → B → C
        let subs = vec![
            make_subtask("A", vec![]),
            make_subtask("B", vec!["A"]),
            make_subtask("C", vec!["B"]),
        ];
        let layers = resolve_execution_order(&subs).unwrap();
        assert_eq!(
            layers,
            vec![
                vec![TaskId("A".to_string())],
                vec![TaskId("B".to_string())],
                vec![TaskId("C".to_string())],
            ]
        );
    }

    #[test]
    fn diamond() {
        // A → C, B → C
        let subs = vec![
            make_subtask("A", vec![]),
            make_subtask("B", vec![]),
            make_subtask("C", vec!["A", "B"]),
        ];
        let layers = resolve_execution_order(&subs).unwrap();
        assert_eq!(layers.len(), 2);
        // First layer: A and B (order may vary)
        assert_eq!(layers[0].len(), 2);
        assert!(layers[0].contains(&TaskId("A".to_string())));
        assert!(layers[0].contains(&TaskId("B".to_string())));
        // Second layer: C
        assert_eq!(layers[1], vec![TaskId("C".to_string())]);
    }

    #[test]
    fn circular_dependency() {
        // A → B → C → A
        let subs = vec![
            make_subtask("A", vec!["C"]),
            make_subtask("B", vec!["A"]),
            make_subtask("C", vec!["B"]),
        ];
        let result = resolve_execution_order(&subs);
        assert!(result.is_err());
    }

    #[test]
    fn mixed_parallel_and_serial() {
        // A → C, B → C, C → D, D → E, F (independent)
        let subs = vec![
            make_subtask("A", vec![]),
            make_subtask("B", vec![]),
            make_subtask("C", vec!["A", "B"]),
            make_subtask("D", vec!["C"]),
            make_subtask("E", vec!["D"]),
            make_subtask("F", vec![]),
        ];
        let layers = resolve_execution_order(&subs).unwrap();
        // Layer 0: A, B, F (all no deps)
        assert_eq!(layers[0].len(), 3);
        // Layer 1: C
        assert_eq!(layers[1], vec![TaskId("C".to_string())]);
        // Layer 2: D
        assert_eq!(layers[2], vec![TaskId("D".to_string())]);
        // Layer 3: E
        assert_eq!(layers[3], vec![TaskId("E".to_string())]);
    }
}
