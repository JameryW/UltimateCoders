# Research: Error Observability

- **Query**: Are there places where errors are silently swallowed without any logging?
- **Scope**: internal
- **Date**: 2026-06-27

## Findings

### Files Found

| File Path | Description |
|---|---|
| `packages/uc-orchestrator/src/orchestrator/grpc-bridge.ts` | gRPC client with reconnect logic |
| `packages/uc-orchestrator/src/orchestrator/orchestrator.ts` | Core orchestrator logic |
| `packages/uc-orchestrator/src/orchestrator/control-signal-subscriber.ts` | NATS control signal handler |
| `packages/uc-orchestrator/src/orchestrator/events.ts` | Event emitter |
| `packages/uc-orchestrator/src/extension.ts` | OMP extension entry point |
| `packages/uc-orchestrator/src/orchestrator/task-store.ts` | Local JSON persistence |

### Silently Swallowed Errors (with file:line references)

#### grpc-bridge.ts

1. **Line 190-191**: `tryReconnect()` catch block -- when reconnect verification (health check) fails after transport recreation, the error is silently swallowed:
   ```typescript
   } catch {
     return false;
   }
   ```
   No logging of *why* the reconnection failed. The caller has no way to know if it was a connection error vs a protocol error vs a timeout.

2. **Line 225-227**: `withReconnect()` inner catch -- when the retry call after a successful reconnect still fails, the error is silently swallowed:
   ```typescript
   try {
     return await fn();
   } catch {
     // Reconnect succeeded but call still failed
   }
   ```
   No logging. The original error that triggered reconnect is also lost (only the fallback value is returned).

3. **Line 206-208**: `checkRestartMarker()` catch block -- file read/parse errors are swallowed:
   ```typescript
   } catch {
     // Marker file doesn't exist or unreadable -- that's fine
   }
   ```
   This is arguably correct (marker file may not exist), but a corrupt marker file would be silently ignored.

4. **Line 249-251**: `health()` retry catch -- after reconnect, second health attempt failure is swallowed:
   ```typescript
   } catch {
     // Reconnect succeeded but health still fails
   }
   ```

5. **Line 278**: `submitTask()` retry catch -- retry after reconnect fails silently:
   ```typescript
   try { return await doSubmit(); } catch { /* retry failed */ }
   ```

#### orchestrator.ts

6. **Line 254**: `createTask()` persist -- `.catch(() => {})` silently swallows persist errors:
   ```typescript
   this.persist(task).catch(() => {});
   ```

7. **Line 463**: `executeWaves()` final writeMemory -- silently swallows:
   ```typescript
   this.bridge.writeMemory(...).catch(() => {});
   ```

8. **Line 573**: `executeWave()` subtask result writeMemory -- silently swallows:
   ```typescript
   this.bridge.writeMemory(...).catch(() => {});
   ```

9. **Line 575-584**: `executeWave()` subtask review writeMemory -- silently swallows.

10. **Line 731**: `pauseTask()` bridge.pauseTask -- silently swallows:
    ```typescript
    this.bridge.pauseTask(taskId).catch(() => {});
    ```

11. **Line 746**: `resumeTask()` bridge.resumeTask -- silently swallows:
    ```typescript
    this.bridge.resumeTask(taskId).catch(() => {});
    ```

12. **Line 1184**: `checkpoint()` writeMemory -- silently swallows:
    ```typescript
    this.bridge.writeMemory(...).catch(() => {});
    ```

13. **Line 1019**: `executeSubtaskWithRetry()` subtask_failed writeMemory -- silently swallows.

14. **Line 1252**: `syncTaskToGrpc()` upsertTask -- silently swallows:
    ```typescript
    this.bridge.upsertTask(this.toPersisted(task)).catch(() => {
      // gRPC sync is best-effort; failure is non-fatal
    });
    ```

15. **Line 1348**: `evictCompletedTasks()` store.removeStale -- silently swallows.

16. **Line 1083-1085**: `executeSubtask()` review catch -- when review fails, the error is swallowed and the subtask is marked completed:
    ```typescript
    } catch {
      result.status = "completed";
    }
    ```

17. **Line 926-928**: `parseSubtaskOutput()` JSON parse catch -- swallowed (text fallback is used, which is intentional).

#### control-signal-subscriber.ts

18. **Line 123-125**: `startNatsSubscription()` JSON parse in message loop -- silently swallows malformed NATS messages:
    ```typescript
    } catch {
      // Malformed JSON -- skip
    }
    ```

19. **Line 131-133**: `startNatsSubscription()` async IIFE catch -- silently swallows subscription end errors:
    ```typescript
    })().catch(() => {
      // Subscription ended
    });
    ```

20. **Line 212-213**: `startPolling()` per-task polling catch -- silently swallows gRPC errors:
    ```typescript
    } catch {
      // gRPC unreachable -- skip
    }
    ```

21. **Line 242**: `checkControlStateChange()` pauseTask handler -- `.catch(() => {})`:
    ```typescript
    this.handler.pauseTask(taskId).catch(() => {});
    ```

22. **Line 250**: `checkControlStateChange()` cancelTask handler -- `.catch(() => {})`.

#### events.ts

23. **Line 83-85**: `OrchestratorEventEmitter.emit()` handler catch -- silently swallows UI rendering errors:
    ```typescript
    } catch {
      // Swallow -- UI rendering errors must not crash orchestration
    }
    ```
    This is intentional design (orchestration must not crash), but errors are not logged at all.

#### extension.ts

24. **Line 60-62**: `restore()` catch -- only warns, does not log the full error:
    ```typescript
    orchestrator.restore().catch((err) => {
      pi.logger.warn(`Failed to restore tasks: ${err}`);
    });
    ```
    This one is actually logged, but uses `warn` which may be missed.

#### task-store.ts

25. **Line 66-74**: `load()` catch -- silently returns null (file doesn't exist is expected, but parse errors are also swallowed):
    ```typescript
    } catch {
      return null;
    }
    ```

26. **Line 77-89**: `loadAll()` catch -- silently returns empty array. Individual file read/parse errors within the loop are also swallowed (line 83).

27. **Line 91-97**: `remove()` catch -- silently ignores removal errors.

28. **Line 114-122**: `loadCheckpoint()` catch -- silently returns null.

29. **Line 127-148**: `removeStale()` -- multiple catch blocks silently swallow errors (lines 137-139 inner, 142-144 outer).

### Code Patterns

**Pattern 1: `.catch(() => {})` on fire-and-forget gRPC calls** -- The most common pattern. Used 10+ times in orchestrator.ts. Rationale: gRPC sync is best-effort. However, zero logging means that persistent gRPC failures are completely invisible to operators.

**Pattern 2: Empty catch blocks in bridge methods** -- `withReconnect` and `tryReconnect` have catch blocks that return fallback values without logging. This makes it impossible to distinguish "server unavailable" from "server returned an application error" from "server returned malformed response".

**Pattern 3: Task-store I/O errors swallowed** -- All file operations in task-store.ts have catch blocks that return null/empty/success. Corrupted JSON files, permission errors, and disk-full conditions are silently hidden.

### Related Specs

- `.trellis/spec/backend/error-handling.md` -- Explicitly states: "Never catch and suppress errors silently -- If an error is non-critical, at minimum use `tracing::warn!()` to log it." The OMP TypeScript code violates this spec consistently.

## Caveats / Not Found

- Some `.catch(() => {})` patterns are genuinely correct for fire-and-forget operations (e.g., gRPC sync on task status change). The issue is lack of logging, not the fire-and-forget pattern itself.
- The events.ts handler catch is intentional to prevent UI errors from crashing orchestration, but logging at debug level would be an improvement.
