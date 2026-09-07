#ifndef EMA_FILTER_H
#define EMA_FILTER_H

#include <stdint.h>

class EMAFilter {
private:
  float emaPrevious{0.0f};
  float alpha{0.2f};
  float alphaDifference{0.8f};
  bool isInitialized{false};
public:
  float filter(float currentValue);
  void reset();
  void setAlpha(float newAlpha);
};
#endif