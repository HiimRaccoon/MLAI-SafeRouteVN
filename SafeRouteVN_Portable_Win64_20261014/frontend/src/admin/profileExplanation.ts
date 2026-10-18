import type { PlanProfile, ProposedAlternative } from "../shared/types/dispatch";

const goal: Record<PlanProfile, string> = {
  FASTEST: "Ưu tiên tổng thời gian di chuyển thấp nhất trong tập tuyến đã tìm được.",
  BALANCED: "Cân bằng 60% thời gian và 40% mức phơi nhiễm rủi ro sau chuẩn hóa; không phải trung bình cộng hai tuyến.",
  SAFER: "Ưu tiên mức phơi nhiễm rủi ro thấp nhất; có thể chấp nhận đi lâu hơn."
};
export function forecastRoutes(proposal?: ProposedAlternative) {
  const routes = proposal?.nativeForecast?.trajectory.vehicle_routes;
  return Array.isArray(routes) ? routes as Record<string, unknown>[] : [];
}
export function physicalRouteKey(proposal: ProposedAlternative): string {
  return JSON.stringify(forecastRoutes(proposal).map(route => ({
    vehicle: route.vehicle_id, route: route.route_column_id ?? route
  })).sort((a, b) => String(a.vehicle).localeCompare(String(b.vehicle))));
}
export function explainProfile(proposal: ProposedAlternative, alternatives: readonly ProposedAlternative[]) {
  if (proposal.origin?.kind !== "MEMBER3" || !proposal.nativeForecast) return null;
  const profile = proposal.origin.profile, metrics = proposal.nativeForecast.metrics.values;
  const peers = alternatives.filter(p => p.nativeForecast && p.origin?.kind === "MEMBER3");
  const identical = peers.filter(p => p.id !== proposal.id && physicalRouteKey(p) === physicalRouteKey(proposal))
    .map(p => p.origin!.kind === "MEMBER3" ? p.origin!.profile : "");
  const fastest = peers.find(p => p.origin?.kind === "MEMBER3" && p.origin.profile === "FASTEST");
  const baseline = fastest?.nativeForecast?.metrics.values;
  const facts = [goal[profile], "Các profile đều ưu tiên phục vụ đơn, độ ưu tiên và giảm trễ trước khi xét thời gian/rủi ro."];
  const time = metrics.total_travel_time_s, risk = metrics.total_exposure;
  if (time != null && risk != null) facts.push(`Thời gian ${time.toFixed(2)} giây; phơi nhiễm ${risk.toFixed(5)} (chỉ số proxy).`);
  if (profile !== "FASTEST" && baseline?.total_travel_time_s != null && baseline.total_exposure != null && time != null && risk != null) {
    const delta = time - baseline.total_travel_time_s, saved = baseline.total_exposure - risk;
    facts.push(`So với FASTEST: ${delta >= 0 ? "lâu hơn" : "nhanh hơn"} ${Math.abs(delta).toFixed(2)} giây; phơi nhiễm ${saved >= 0 ? "giảm" : "tăng"} ${Math.abs(saved).toFixed(5)}${baseline.total_exposure > 0 ? ` (${(Math.abs(saved) / baseline.total_exposure * 100).toFixed(2)}%)` : ""}.`);
  }
  if (identical.length) facts.push(`Cùng tuyến với ${identical.join(", ")}. Các mục tiêu hiện chọn cùng nghiệm; đây không phải một phương án đường đi khác.`);
  else if (peers.length < 3) facts.push("Đang chờ các profile còn lại để đối chiếu đầy đủ.");
  else facts.push("Tuyến khác các profile còn lại; hãy đối chiếu thời gian và mức phơi nhiễm trước khi chọn.");
  facts.push(`${proposal.nativeForecast.jobView.served_orders.length} đơn được phục vụ, ${proposal.nativeForecast.jobView.unserved_orders.length} đơn chưa được phục vụ. Kết quả là dự báo trong tập ứng viên hữu hạn, chưa chứng minh tối ưu toàn cục.`);
  return { facts, identical };
}
