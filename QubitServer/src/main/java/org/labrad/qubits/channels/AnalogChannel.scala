package org.labrad.qubits.channels

import org.labrad.qubits.FpgaModel
import org.labrad.qubits.FpgaModelAnalog
import org.labrad.qubits.channeldata.AnalogData
import org.labrad.qubits.channeldata.AnalogDataFourier
import org.labrad.qubits.enums.DacAnalogId
import org.labrad.qubits.util.ComplexArray
import scala.collection.JavaConverters._

class AnalogChannel(name: String) extends SramChannelBase[AnalogData](name) {

  private var dacId: DacAnalogId = null

  clearConfig()

  def setDacId(id: DacAnalogId): Unit = {
    dacId = id
  }

  def getDacId(): DacAnalogId = {
    dacId
  }

  override def setFpgaModel(fpga: FpgaModel): Unit = {
    fpga match {
      case analogDac: FpgaModelAnalog =>
        this.fpga = analogDac
        analogDac.setAnalogChannel(dacId, this)

      case _ =>
        sys.error(s"AnalogChannel '$getName' requires analog board.")
    }
  }

  /**
   * Add data to the current block.
   * @param data
   */
  def addData(data: AnalogData): Unit = {
    val expected = fpga.getBlockLength(currentBlock)
    data.setChannel(this)
    data.checkLength(expected)
    blocks.put(currentBlock, data)
  }

  def getBlockData(name: String): AnalogData = {
    blocks.get(name) match {
      case null =>
        // create a dummy data set with zeros
        val len = fpga.getBlockLength(name)
        val fourierLen = if (len % 2 == 0) len/2 + 1 else (len+1) / 2
        val zeros = Array.ofDim[Double](fourierLen)
        val data = new AnalogDataFourier(new ComplexArray(zeros, zeros), 0, true, false)
        data.setChannel(this)
        blocks.put(name, data)
        data

      case data => data
    }
  }

  def getSramData(name: String): Array[Int] = {
    blocks.get(name).getDeconvolved()
  }


  //
  // Configuration
  //

  private var settlingRates: Array[Double] = null
  private var settlingAmplitudes: Array[Double] = null
  private var reflectionRates: Array[Double] = null
  private var reflectionAmplitudes: Array[Double] = null

  def clearConfig(): Unit = {
    settlingRates = Array.empty[Double]
    settlingAmplitudes = Array.empty[Double]
    reflectionRates = Array.empty[Double]
    reflectionAmplitudes = Array.empty[Double]
  }

  def setSettling(rates: Array[Double], amplitudes: Array[Double]): Unit = {
    require(rates.length == amplitudes.length,
        s"$getName: lists of settling rates and amplitudes must be the same length")
    settlingRates = rates
    settlingAmplitudes = amplitudes
    // mark all blocks as needing to be deconvolved again
    for (block <- blocks.values().asScala) {
      block.invalidate()
    }
  }

  def getSettlingRates(): Array[Double] = {
    settlingRates.clone()
  }

  def getSettlingTimes(): Array[Double] = {
    settlingAmplitudes.clone()
  }

  def setReflection(rates: Array[Double], amplitudes: Array[Double]): Unit = {
    require(rates.length == amplitudes.length,
        s"$getName: lists of reflection rates and amplitudes must be the same length")
    reflectionRates = rates;
    reflectionAmplitudes = amplitudes;
    // mark all blocks as needing to be deconvolved again
    for (block <- blocks.values().asScala) {
      block.invalidate()
    }
  }

  def getReflectionRates(): Array[Double] = {
    reflectionRates.clone()
  }

  def getReflectionAmplitudes(): Array[Double] = {
    reflectionAmplitudes.clone()
  }
}
