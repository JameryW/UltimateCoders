/** Gateway enum names use PascalCase; Dashboard state uses snake_case. */
export function normalizeGrpcStatus(status: string): string {
  return status.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
}
