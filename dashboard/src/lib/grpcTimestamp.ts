/** TaskProto timestamps are Unix seconds, as specified by the Gateway. */
export function taskTimestampToISO(seconds: bigint): string {
  return new Date(Number(seconds) * 1000).toISOString();
}

/** Event producers use ISO strings or numeric epoch seconds/milliseconds/microseconds. */
export function eventTimestampToISO(value: string | number | bigint): string {
  if (typeof value === "string" && !/^\d+(\.\d+)?$/.test(value)) return value;
  const epoch = Number(value);
  const milliseconds = epoch >= 1e15 ? epoch / 1000 : epoch >= 1e12 ? epoch : epoch * 1000;
  return new Date(milliseconds).toISOString();
}
