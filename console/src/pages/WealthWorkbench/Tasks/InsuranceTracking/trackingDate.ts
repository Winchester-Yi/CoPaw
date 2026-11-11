import dayjs from "dayjs";
export const DATE_FORMAT = "YYYY-MM-DD";
export const TIME_VALUE_FORMAT = "YYYY-MM-DDTHH:mm";
export function trackingNow(withTime = false) {
  return dayjs().format(withTime ? TIME_VALUE_FORMAT : DATE_FORMAT);
}
