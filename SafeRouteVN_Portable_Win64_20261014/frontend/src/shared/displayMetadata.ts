const orderFields = {customerName:160,customerPhone:64,address:500,notes:2000};
const vehicleFields = {driverName:160,driverPhone:64,licensePlate:40,vehicleModel:120};
function read<T extends Record<string,number>>(value:unknown, fields:T):Partial<Record<keyof T,string>> {
 if(value===undefined)return {};
 if(!value || typeof value!=='object' || Array.isArray(value))throw new Error('Invalid display metadata');
 const source=value as Record<string,unknown>, result:Record<string,string>={};
 for(const [key,max] of Object.entries(fields)) {
  const text=source[key]; if(text==null)continue;
  if(typeof text!=='string'||text.length>max||/[\x00-\x08\x0b-\x1f]/.test(text))throw new Error(`Invalid display field ${key}`);
  if(text.trim())result[key]=text.trim();
 }
 return result as Partial<Record<keyof T,string>>;
}
export const orderDisplay=(value:unknown)=>read(value,orderFields);
export const vehicleDisplay=(value:unknown)=>read(value,vehicleFields);
