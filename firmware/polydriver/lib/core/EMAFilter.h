#ifndef EMA_FILTER_H
#define EMA_FILTER_H

class EMAFilter {
public:
  static constexpr float DEFAULT_ALPHA = 1.0f;

  float filter(float sample);
  void reset();
  bool setAlpha(float newAlpha);
  float getAlpha() const { return alpha; }

private:
  float alpha{DEFAULT_ALPHA};
  float oneMinusAlpha{1.0f - DEFAULT_ALPHA};
  float lastOutput{0.0f};
  bool isInitialized{false};
};

#endif
