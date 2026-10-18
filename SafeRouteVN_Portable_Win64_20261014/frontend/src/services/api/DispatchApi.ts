import type { DispatchSnapshot } from "../../shared/types/dispatch";
import type { ScenarioId } from "../../shared/types/scenario";

export interface DispatchApi {
  profileExplanations?(sid:string,cid:string,signal?:AbortSignal):Promise<import('../../integrations/member3/types').M3Explanations>;
  scenarioTemplate?():Promise<Record<string,unknown>>;
  importedScenarios?():Promise<{imports:import('../../integrations/member3/types').M3Run[]}>;
  importNativeScenario?(body:{request_id:string;name:string;fixture:Record<string,unknown>}):Promise<import('../../integrations/member3/types').M3Run>;
  deliveryDay?(action:string,data?:Record<string,unknown>):Promise<DispatchSnapshot>;
  createWeatherRun?(input: {scenarioId: ScenarioId; eventAfterMinutes: number; requestId: string}): Promise<import('../../integrations/member3/types').M3Run>;
  weatherRun?(id: string): Promise<import('../../integrations/member3/types').M3Run>;
  loadWeatherRun?(id: string, requestId: string): Promise<DispatchSnapshot>;
  triggerAndReplan?(eventId: string): Promise<DispatchSnapshot>;
  replayToEvent?(eventId: string): Promise<DispatchSnapshot>;
  readonly presentation?: import("../../shared/presentation").DispatchPresentation;
  capabilities?(): Promise<import("../../integrations/member3/capabilities").DispatchCapabilities>;
  cancelComparison?(): Promise<DispatchSnapshot>;
  getSnapshot(): Promise<DispatchSnapshot>;
  subscribe(listener: (snapshot: DispatchSnapshot) => void): () => void;
  loadScenario(id: ScenarioId): Promise<DispatchSnapshot>;
  optimize(): Promise<DispatchSnapshot>;
  selectAlternative(planId: string): Promise<DispatchSnapshot>;
  acceptSelectedPlan(): Promise<DispatchSnapshot>;
  applyEvent?(eventId: string): Promise<DispatchSnapshot>;
  replayStep?(): Promise<DispatchSnapshot>;
  replayStart?(speed?: import('../../shared/replaySpeeds').ReplaySpeed): Promise<DispatchSnapshot>;
  replaySpeed?(speed: import('../../shared/replaySpeeds').ReplaySpeed): Promise<DispatchSnapshot>;
  replayPause?(): Promise<DispatchSnapshot>;
  resetSession?(): Promise<DispatchSnapshot>;
  setAutoDeliver?(enabled: boolean): Promise<DispatchSnapshot>;
  triggerFixtureEvent(eventId: string): Promise<DispatchSnapshot>;
  setUrgentOrderEnabled(enabled: boolean): Promise<DispatchSnapshot>;
  pickupOrder(input: { vehicleId: string; orderId: string }): Promise<DispatchSnapshot>;
  deliverOrder(input: { vehicleId: string; orderId: string }): Promise<DispatchSnapshot>;
  advanceDemoClock(minutes: number): Promise<DispatchSnapshot>;
  resetDemoSession(): Promise<DispatchSnapshot>;
  importScenario?(value: unknown): Promise<DispatchSnapshot>;
  moveOrder?(id: string, latitude: number, longitude: number): Promise<DispatchSnapshot>;
}
