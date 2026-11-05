// WC-006: the Work Command Center UI is gated by the `work_center_v1` health capability. Stub-API handlers are wrapped so a test
// states the flag explicitly; `withFlag(handler, false)` is the flag-off (pre-WC) server.
export const WORK_CENTER_CAPABILITY='work_center_v1';
export const withFlag=(handler,on=true)=>(p,url)=>{
 if(p==='/api/v1/health')return {status:'ok',capabilities:['mission_activity_v1','ci_v1','github_v1',...(on?[WORK_CENTER_CAPABILITY]:[])]};
 return handler(p,url);
};
